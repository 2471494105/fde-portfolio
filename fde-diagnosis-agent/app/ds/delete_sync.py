"""删除与更新的决策部分：给定本地状态和这一次的上游快照，决定谁该标删、谁该复活。

只做决策，不碰数据库（和 `incremental.py` 一样的分层）。因为它错了会非常安静：
删多了，客户的数据在系统里凭空消失；删少了，过期数据永远在回答问题。两者都不报错。

## 「不在快照里」不可判定

上游不发 tombstone 时，一行不在快照里只有两种解释：它被删了，或者这次没取全
（分页超时、接口限流、权限变更）。两者在数据上完全一样。所以三条硬规则：

1. **第一次缺席只记账，不删。** 连续缺席达到阈值（默认 2 次）才判删除。
2. **快照骤降要整轮拒绝。** 上游挂了返回空表时，「差额」等于全库，照规则跑就是
   一次把客户数据全软删掉。行数掉到上轮的 `min_ratio` 以下，本轮不产生任何删除决策。
3. **有删除事件时不信缺席次数。** 明确 tombstone 是最强证据，立刻软删。
   但「最强」不等于可以越过下面两条：事件不能删掉有未闭环引用的设备，
   也不能删掉这一次还出现在快照里的设备——后者是两个信号自相矛盾，选「不删」。

## 三种不删

- **有未闭环引用**（未完成工单指向这台设备）→ `protect`，转人工。删了它就是悬空引用。
  **这条优先于删除事件**：事件只说明上游那边没了，不说明本地工单已经闭环。
- **已是软删状态**又缺席 → `noop`，不重复删、不刷 reason。
- **本地没有、只在快照里出现的 id** → 不产生决策，那是新增路径的事。
- **信号矛盾**：既在快照里、又收到删除事件 → `noop`，等下一次快照自己消除，或转人工。

## 删除必须可撤销

上游回补、或上一轮判断错了，重新出现在快照里就是 `revive`：清软删标记、缺席计数归零。
**只做单向删除的同步，第一次误判之后就会永久错下去。**

## 物理行不删

软删只改状态表，落地表的行原样留着——因为证据、工单、审计可能还引用它。
真删是保留期任务的事，不在这条路径上。
"""

ABSENT_CONFIRM = 2
MIN_SNAPSHOT_RATIO = 0.5


def _to_set(ids):
    if ids is None:
        return set()
    if isinstance(ids, dict):
        return set(ids)
    return {r["device_id"] if isinstance(r, dict) else r for r in ids}


def plan_deletes(local_ids, snapshot_ids, states=None, protected=None, events=None,
                 threshold=ABSENT_CONFIRM, min_ratio=MIN_SNAPSHOT_RATIO):
    """返回 {"aborted": bool, "reason": str, "decisions": [ {...}, ... ]}。

    local_ids    : 本地已装载的设备主键集合（落地表里存在的）
    snapshot_ids : 这一次上游全量快照的主键集合
    states       : {id: {"absent_count": int, "deleted": bool}} 本地已记的状态
    protected    : 有未闭环引用的 id 集合，一律不删
    events       : {id: reason} 上游明确下发的删除事件

    决策动作只有五种：mark_absent / soft_delete / revive / protect / noop。
    """
    local = _to_set(local_ids)
    snap = _to_set(snapshot_ids)
    states = states or {}
    protected = _to_set(protected)
    events = dict(events or {})

    # 护栏一：快照骤降，整轮拒绝。这条比任何单行判断都重要。
    if local and len(snap) < len(local) * min_ratio:
        return {
            "aborted": True,
            "reason": (
                f"快照只有 {len(snap)} 条，本地 {len(local)} 条，低于 {min_ratio:.0%} 阈值；"
                "更像接口没取全而不是真的删了，本轮不做任何删除判定"
            ),
            "decisions": [],
        }

    def _state(did):
        s = states.get(did) or {}
        return int(s.get("absent_count") or 0), bool(s.get("deleted"))

    decisions = []
    for did in sorted(local | set(events), key=str):
        n, deleted = _state(did)
        ev = events.get(did)

        # 1. 在快照里：上游这一次还给了它。
        #    即使同时收到删除事件也不删——两个信号自相矛盾时选「不删」，
        #    因为删除不可逆，而矛盾下一次快照就可能自己消除。
        if did in snap:
            if ev:
                decisions.append({"id": did, "action": "noop", "absent_count": 0,
                                  "reason": f"快照里仍有它，却又收到删除事件（event:{ev}），"
                                            "信号矛盾，本轮不删"})
            elif deleted or n:
                decisions.append({"id": did, "action": "revive",
                                  "reason": "重新出现在快照里", "absent_count": 0})
            continue

        # 2. 有未闭环引用：不管是不在快照还是收到删除事件，都不删。
        #    顺序很关键——事件是「最强证据」，但最强不等于能删掉有活工单的设备。
        if did in protected:
            why = (f"上游下发删除事件（event:{ev}），但本地有未闭环引用"
                   if ev else "不在快照，且本地有未闭环引用")
            decisions.append({"id": did, "action": "protect",
                              "reason": f"{why}，转人工", "absent_count": n + 1})
            continue

        # 3. 明确删除事件：不信缺席次数，立刻软删
        if ev:
            decisions.append({"id": did, "action": "soft_delete",
                              "reason": f"event:{ev}", "absent_count": 0})
            continue

        # 4. 已经软删过又缺席：不重复删
        if deleted:
            decisions.append({"id": did, "action": "noop",
                              "reason": "已是软删状态", "absent_count": n})
            continue

        # 5. 不在快照：清点缺席
        n1 = n + 1
        if n1 < threshold:
            decisions.append({"id": did, "action": "mark_absent",
                              "reason": f"第 {n1} 次缺席，未达阈值 {threshold}",
                              "absent_count": n1})
        else:
            decisions.append({"id": did, "action": "soft_delete",
                              "reason": f"连续缺席 {n1} 次", "absent_count": n1})

    return {"aborted": False, "reason": "", "decisions": decisions}


def content_changed(old_hash, new_hash):
    """更新判定的最小形态：指纹不同才算改了，不靠字段逐个比。

    两种情况都不判「改了」：
    - 上游这次没给指纹（`new_hash is None`）：没有可比的东西。
    - 本地还没有基线指纹（首次装载、或历史行没记过）：先补记录，不产生一次 update。
      否则第一次同步会把每一行都判成「更新」，日志全废。
    """
    if new_hash is None or not old_hash:
        return False
    return old_hash != new_hash
