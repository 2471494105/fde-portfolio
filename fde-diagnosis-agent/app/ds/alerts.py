"""中控台的事件规划：把「判据跑出来的红」变成「一件一件要人处理的活」。

这个模块不连库，只做判断，理由和 checks.py 一样：能不能叫醒人，是一条规则，
必须能在单测里红一次，而不是写在某个页面的 if 里。

三条克制规则（它们是这个模块存在的全部理由）：
1. 同一批红行**再跑一次不叫**。只有「从绿转红」或「新增了行」才值得推。
2. 变绿不写「已修复」，只写「复跑归零」——修没修好由复跑证明，不由人点按钮证明。
3. 判据静默单独一类：它红的是「没人看」，不是数据。混进数据问题里会让人
   去洗一批本来没错的数据。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

# 中控台上按判据分组用的短名：客户看得懂的是「这批单子归属不明」，不是 check_id
TITLES = {
    "station_contract": "工单/缺陷无法唯一归属工位",
    "status_policy": "看板状态与流程口径对不上",
    "defect_fields": "缺陷记录字段越界或空批次",
    "alarm_ack": "报警挂着没人接、也没人销",
    "source_schema": "源端表结构与登记对不上",
    "source_values": "源端数值越界或量纲错位",
    "value_domain": "数值量纲或枚举值越界",
    "data_contract": "脏数据被挡在主表外、或契约断言不达标",
    # 扫描自己也是一条要被观测的判据：它停了，中控台上的数字就不再代表现在
    "alert_sweep": "事件扫描（中控台自检）",
}

# 每道判据的默认责任方（与登记表 owner 一致，缺省写「待指派」而不是编一个人）
OWNERS = {
    "station_contract": "产线计划员",
    "status_policy": "车间主管",
    "defect_fields": "质量工程师",
    "alarm_ack": "值班人",
    "source_values": "质量工程师",
    "source_schema": "数据管理员",
    "data_contract": "数据管理员",
    "alert_sweep": "值班人",
}

SILENT_HOURS = 24

# 标题里的量词。判据的红不是都用「行」数：契约是「失败断言 + 隔离区行」的件数，
# 值域是「越界列」数。写成「行」会在客户面前立刻被抓住——数字对不上口径。
UNITS = {
    "data_contract": "件",
    "source_values": "列",
    "status_policy": "条",
    "alarm_ack": "条",
}

# 类别登记表是「客户怎么讲这件事」的唯一来源：事件里带过去，页面不再自己拼名字
try:  # app.ds.alerts / 直接跑脚本两种入口
    from .issues import category_for, is_external
except ImportError:  # pragma: no cover
    from app.ds.issues import category_for, is_external


def issue_fields(check_id: str) -> dict:
    """把类别信息塞进 detail：页面只读，不重算。没登记就留空，不编一个类别。"""
    c = category_for(check_id)
    if not c:
        return {}
    return {
        "issue": c["issue_id"],
        "issue_short": c["short"],
        "issue_name": c["name"],
        "first_step": c["first_step"],
    }


def diff_ids(prev: Iterable[Any] | None, cur: Iterable[Any] | None) -> dict[str, list[str]]:
    """按编号比集合，不比条数：5 对 5 可能是一换一，那种「没变化」是假绿。"""
    p = {str(x) for x in (prev or [])}
    c = {str(x) for x in (cur or [])}
    return {
        "new": sorted(c - p, key=lambda s: (len(s), s)),
        "gone": sorted(p - c, key=lambda s: (len(s), s)),
        "same": sorted(p & c, key=lambda s: (len(s), s)),
    }


def _title(check_id: str, n: int, delta: int | None = None) -> str:
    base = TITLES.get(check_id, check_id)
    unit = UNITS.get(check_id, "行")
    if delta is None:
        return f"{base}：{n} {unit}"
    return f"{base}：{n} {unit}（新增 {delta}）"


def plan_transition(
    check_id: str,
    prev: dict | None,
    cur: dict | None,
    *,
    owner: str | None = None,
) -> dict[str, list[dict]]:
    """对比同一道判据的相邻两次运行，产出「要开的事件」和「要关的事件」。

    prev / cur 形状：{"run_id": int, "ran_at": str|datetime, "status": str,
                      "red_count": int, "ids": [..]}
    prev 为 None 表示首次运行：有红就是新事件，但要标 first=true，
    页面上显示「首发」，避免把库里本来就有的老问题说成「今天新增」。
    """
    owner = owner or OWNERS.get(check_id) or "待指派"
    events: list[dict] = []
    closes: list[dict] = []

    cur = cur or {}
    cur_n = int(cur.get("red_count") or 0)
    prev_n = int(prev.get("red_count") or 0) if prev else 0
    cur_ids = cur.get("ids")
    prev_ids = prev.get("ids") if prev else None
    # 两边都有行编号才比得出来。历史台账可能只存条数（T22 之前），
    # 那种情况不许假装「新增 0」——它只是没得比。
    comparable = cur_ids is not None and prev_ids is not None
    d = diff_ids(prev_ids, cur_ids) if comparable else {"new": [], "gone": [], "same": []}
    first = prev is None or not comparable

    key_red = f"{check_id}:red"
    key_silent = f"{check_id}:silent"

    if cur.get("status") == "error":
        events.append(
            {
                "dedup_key": f"{check_id}:self_error",
                "check_id": check_id,
                "kind": "check_red",
                "severity": "amber",
                "title": f"{TITLES.get(check_id, check_id)}：判据自己跑挂了",
                "detail": {"run_id": cur.get("run_id"), "error": cur.get("error") or "",
                           **issue_fields(check_id)},
                "owner": owner,
                "notify": True,
            }
        )
        return {"events": events, "closes": closes}

    if cur_n > 0:
        grew = cur_n > prev_n or bool(d["new"])
        # 判据自己判「黄」时（warn：有越界但没到拒跑），事件也必须是黄：
        # 把黄写成红，客户第一次看到就会问「你不是说这只是提醒吗」。
        sev = "amber" if cur.get("status") == "warn" else "red"
        event = {
            "dedup_key": key_red,
            "check_id": check_id,
            "kind": "check_red",
            "severity": sev,
            "title": _title(check_id, cur_n, len(d["new"]) if comparable else None),
            "detail": {
                "run_id": cur.get("run_id"),
                "red_count": cur_n,
                "first": first,
                "baseline": not comparable,
                "new_ids": d["new"],
                "gone_ids": d["gone"],
                "same_count": len(d["same"]),
                **issue_fields(check_id),
            },
            "owner": owner,
            "notify": bool(first or grew),
        }
        events.append(event)
        closes.append({"dedup_key": key_silent, "reason": "判据已恢复运行"})
    else:
        if prev_n > 0:
            closes.append({"dedup_key": key_red, "reason": "复跑归零（0 行违约，未经人工声明）"})
        closes.append({"dedup_key": key_silent, "reason": "判据在跑"})
    return {"events": events, "closes": closes}


def silent_events(
    last_runs: dict[str, dict],
    now: datetime,
    *,
    expected: Iterable[str] | None = None,
    threshold_hours: int = SILENT_HOURS,
    owners: dict[str, str] | None = None,
) -> list[dict]:
    """超过阈值没跑的判据：一类单独的事件。没登记过运行记录的判据也算静默。"""
    expected = list(TITLES) if expected is None else list(expected)
    owners = owners or OWNERS
    out: list[dict] = []
    for check_id in expected:
        last = last_runs.get(check_id)
        if last is None:
            hours = None
        else:
            ran_at = last.get("ran_at")
            if isinstance(ran_at, str):
                ran_at = datetime.fromisoformat(ran_at.replace(" ", "T", 1))
            hours = (now - ran_at).total_seconds() / 3600.0 if ran_at else None
        if hours is not None and hours < threshold_hours:
            continue
        out.append(
            {
                "dedup_key": f"{check_id}:silent",
                "check_id": check_id,
                "kind": "check_silent",
                "severity": "amber",
                "title": (
                    f"{TITLES.get(check_id, check_id)}：从没跑过"
                    if hours is None
                    else f"{TITLES.get(check_id, check_id)}：{hours:.0f} 小时没复跑"
                ),
                "detail": {"hours": None if hours is None else round(hours, 1), "threshold": threshold_hours,
                           # 静默不是数据问题，类别固定归「判据没人复跑」，别混进数据类
                           **issue_fields("alert_sweep")},
                "owner": owners.get(check_id) or "待指派",
                "notify": True,
            }
        )
    return out


def overdue_events(orders: list[dict], today: date, *, owners: dict[str, str] | None = None) -> list[dict]:
    """整改单逾期：单子到期但没关，是「有人接了活但没干完」，与数据红分开。"""
    owners = owners or {}
    out = []
    for o in orders:
        if (o.get("state") or "") in ("closed", "done"):
            continue
        due = o.get("due_on")
        if isinstance(due, str):
            due = date.fromisoformat(due)
        if not due or due >= today:
            continue
        out.append(
            {
                "dedup_key": f"{o.get('rm_no')}:overdue",
                "check_id": "remediation",
                "kind": "order_overdue",
                "severity": "red",
                "title": f"整改单 {o.get('rm_no')} 逾期 {(today - due).days} 天未关",
                "detail": {
                    "rm_no": o.get("rm_no"),
                    "due_on": due.isoformat(),
                    "violation_count": o.get("violation_count"),
                    "table": o.get("table_name"),
                    "field": o.get("field_name"),
                    **issue_fields("remediation"),
                },
                "owner": o.get("owner") or owners.get("remediation") or "待指派",
                "notify": True,
            }
        )
    return out


def summarize(rows: list[dict]) -> dict:
    """页面顶部那行数字：未处理多少、其中红多少、有没有人认领。"""
    open_rows = [r for r in rows if (r.get("state") or "open") != "closed"]
    return {
        "open": len(open_rows),
        "red": sum(1 for r in open_rows if r.get("severity") == "red"),
        "amber": sum(1 for r in open_rows if r.get("severity") == "amber"),
        "acked": sum(1 for r in open_rows if r.get("state") == "acked"),
    }
