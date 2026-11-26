"""事实层的决策部分：给定「现在认定的事实」和「这一轮拿到的值」，决定谁该关、谁该开。

只做决策，不碰数据库（和 `incremental.py` / `delete_sync.py` 同一分层）。
落库在 `scripts/check_facts.py`：那里负责事务、时间戳和 advisory lock。

## 为什么需要这一层

`stg_devices` 每轮同步被覆盖，一张表只有一个「现在」。于是三个问题都答不了，
而且都不报错：

- 三个月后问「当时 CX-101 在哪条线」——库里只剩新值，只能答「现在是 1号线B」，
  没有任何数据能证明当时是 1号线；
- 客户拿一条投诉来复现，我们拿今天的设备状态去跑昨天的判定，跑出来的结果是假的；
- 一台设备一个月换三次线、想算「换线后 NG 率变化」——算不了。

所以这条路径的判据不是「有没有历史表」，而是**改了一个值之后，旧值还在不在，
且说得清它什么时候结束**。

## 三条硬规则

1. **一个键同时只能有一条当前值**（`valid_to IS NULL`）。由部分唯一索引兜底，
   代码不是唯一防线——代码写错时数据库必须拒绝第二次写入，否则表只是装饰。
2. **值没变就不产生新行**。判 `value_hash`，不逐字段比。逐字段比的结果是：
   表加一个属性、比较逻辑就漏一处，漏一处就「永远不更新」且没有任何症状。
3. **快照没给 ≠ 值为空**。`incoming` 里缺的键保持当前值不动，**绝不关掉**。
   这和 `resync_cols` 是同一条原则：把「上游这次没给」当成「这个值没了」，
   一晚上就能把历史抹平。

## 时间只能记到货时刻（K-3 在事实层的原样重演）

上游没有「这个值从什么时候开始成立」这件事。我们只知道「这一轮同步跑到了它」。
所以 `valid_from` 只能填同步时刻，并登记 `valid_from_basis='ingested'`：

- 两次快照之间的中间状态不可见（改了又改回，我们只看到「没变」）；
- `valid_from` 的精度等于同步频率，每天一次就是天级；
- 补录数据晚到时会被记成「刚到货」，而不是「发生在过去」。

`valid_from_basis` 这一列不是为了将来能填 `effective` 才留的——它是为了让**现在这批
数据的时间口径可以被查询识别**，而不是被下游当成发生时刻直接拿去算。
"""

import hashlib
import json
from datetime import date, datetime

# 参与版本化的设备属性。刻意不含 device_id：它是实体键（entity_id），不是属性。
# 这份清单必须与 stg_devices 的业务列一致；表里多一列而这里没跟，
# 那一列就永久没有历史——`scripts/check_facts.py` 第 [6] 步会拿表结构核对。
FACT_KEYS = (
    "line",
    "station",
    "camera_model",
    "lens_model",
    "light_model",
    "install_date",
    "maintenance_cycle_days",
)

# valid_from 的口径只允许这两种。没有「不确定」——不确定就该去核实，
# 而不是登记一个模糊值让下游自己猜。
VALID_FROM_BASIS = ("ingested", "effective")
DEFAULT_BASIS = "ingested"


def fact_value(v):
    """把库里的值转成能放进 jsonb 的形态。

    日期转 ISO 字符串：直接塞 datetime 会被 json 序列化成随 locale 变化的串，
    而这张表要跨年保存，「2025-03-12」比「Mar 12 2025」活得久。
    """
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def fact_hash(v):
    """值的指纹：判「变没变」，不判「怎么变」。

    sort_keys 保证 dict 键序不影响结果；default=str 兜住 date 之类的非 JSON 类型，
    避免调用方忘了先 `fact_value()` 时直接抛异常——判等失败退化成漏更新，
    比抛异常更危险，所以这里选择「能算就算」。
    """
    s = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def rows_to_facts(rows, keys=FACT_KEYS, id_key="device_id"):
    """把一批当前值摊平成 `{(entity_id, fact_key): value}`。

    两道跳过，都是同一个意思——「这一轮没告诉我们这个值」：

    - 列不在 row 里（CSV 源、外部接口会这样）；
    - 值为 `None`（pg 落地表会这样：`SELECT *` 永远给出所有列，上游少给一列就是 NULL）。

    第二道是踩出来的：`stg_devices` 没有约束，少给一列的落地结果就是 NULL。第一版
    把 NULL 当成「值变了」，一次只给两列的上游就把其余 6 个属性判成「清空」——
    关掉旧值再写入 NULL，历史当场被抹平。**在 pg 源里 NULL 是语义黑洞**：
    它同时表示「没给」和「给了空值」，落地表这一层分不出来。要表达「真的清空」，
    必须改源端协议（空串、显式 tombstone），不能靠把 NULL 塞进这张表。

    空 device_id 的行直接跳过——没有实体，事实无处挂。
    """
    out = {}
    for r in rows:
        eid = r.get(id_key)
        if eid is None or str(eid).strip() == "":
            continue
        for k in keys:
            if k in r and r.get(k) is not None:
                out[(str(eid), k)] = fact_value(r.get(k))
    return out


def plan_facts(current, incoming, tenant="client_a", source="sync",
               basis=DEFAULT_BASIS):
    """返回 `{"closes", "opens", "noops", "uncovered"}`。

    current  : `{(entity_id, fact_key): {"id": int, "value_hash": str, "value": any}}`
               来自 `facts.device_fact_current`
    incoming : `{(entity_id, fact_key): value}`
               来自 `rows_to_facts()`，即这一轮快照摊平后的值

    四种产出：

    - `opens`  ：要插入的新行（首次出现的键，或改值后的新值）
    - `closes` ：要写 `valid_to` 的旧行（只给 id，时间由落库层填 now()）
    - `noops`  ：值没变，不产生新行（这是「历史表不被自己的重复同步撑爆」的机制）
    - `uncovered`：current 里有、这一轮没给的键。**不关**，但要露出来——
      它可能是「上游少给了」，也可能是「设备该淘汰了」，这两种都不该由同步路径决定。

    `supersedes` 只在改值时出现：链条断了就看不出「从 1号线 改到 1号线B」，
    只看得到两个孤立的值。
    """
    if basis not in VALID_FROM_BASIS:
        raise ValueError(
            f"valid_from_basis 非法：{basis!r}（只接受 {'/'.join(VALID_FROM_BASIS)}）"
        )

    closes, opens, noops = [], [], []

    for (eid, fk), val in sorted(incoming.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        h = fact_hash(val)
        old = current.get((eid, fk))
        if old is None:
            opens.append({
                "tenant": tenant, "entity_id": eid, "fact_key": fk,
                "value": val, "value_hash": h,
                "valid_from_basis": basis, "supersedes": None, "source": source,
            })
        elif old.get("value_hash") == h:
            noops.append({"entity_id": eid, "fact_key": fk, "id": old.get("id")})
        else:
            closes.append({"id": old.get("id"), "entity_id": eid, "fact_key": fk,
                           "old_hash": old.get("value_hash")})
            opens.append({
                "tenant": tenant, "entity_id": eid, "fact_key": fk,
                "value": val, "value_hash": h,
                "valid_from_basis": basis, "supersedes": old.get("id"),
                "source": source,
            })

    uncovered = [
        {"entity_id": eid, "fact_key": fk, "id": cur.get("id")}
        for (eid, fk), cur in sorted(current.items(), key=lambda kv: (kv[0][0], kv[0][1]))
        if (eid, fk) not in incoming
    ]

    return {"closes": closes, "opens": opens, "noops": noops, "uncovered": uncovered}


def plan_issues(plan):
    """决策结果自身的自洽检查：不连库就能判的那几条。

    它在 `scripts/check_facts.py` 里每轮跑，也在 `scripts/data_health.py` 里被复核。
    """
    out = []
    for c in plan.get("closes") or []:
        if c.get("id") is None:
            out.append(f"{c.get('entity_id')}.{c.get('fact_key')} 要关旧行却没有 id")
    for o in plan.get("opens") or []:
        if not o.get("value_hash"):
            out.append(f"{o.get('entity_id')}.{o.get('fact_key')} 新行没有值指纹")
        if o.get("valid_from_basis") not in VALID_FROM_BASIS:
            out.append(
                f"{o.get('entity_id')}.{o.get('fact_key')} 的 valid_from_basis "
                f"是 {o.get('valid_from_basis')!r}"
            )
    # 改值必须成对出现：只关不开 = 这个键从此没有当前值；开了不记 supersedes = 链条断了
    opened = {(o.get("entity_id"), o.get("fact_key")): o for o in plan.get("opens") or []}
    closed_keys = {(c.get("entity_id"), c.get("fact_key")) for c in plan.get("closes") or []}
    for k in sorted(closed_keys):
        o = opened.get(k)
        if o is None:
            out.append(f"{k[0]}.{k[1]} 关了旧行但没开新行：这个键会没有当前值")
        elif o.get("supersedes") is None:
            out.append(f"{k[0]}.{k[1]} 开了新行但没记 supersedes：看不出「从哪个值改过来」")
    return out
