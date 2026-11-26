"""行级字段判据：缺陷表现在靠页面染色，Python 侧一条判据都没有。

为什么单独一族、不塞进 checks.py
--------------------------------
`app/ds/checks.py` 的 `run_check` 是为「状态词折法」设计的：它的 ctx 里是客户词表、
看板关闭词这些**按客户不同**的东西，`check_applies()` 判的也是「这道判据对这家客户成不成立」。
缺陷表这三条规则没有客户差异——0–100 就是 0–100，未来时间就是未来时间；硬塞进同一个
registry，会让 E 判据拿工单行去喂它们（字段根本不存在），判出来的东西没有意义。

所以分成第二族：同一份登记（`data/mapping/field_rules.json`）驱动、同一个入口
`run()`、返回值同样带 `ran`。`scripts/check_status_policy.py` 的 F（入口绕过）扫描范围
已扩到这一族——「判据只能从入口跑」的约束不因分族而放松。

已知边界
--------
- 规则只判「这一行对不对」，不判「这一行该不该存在」；
- `conf_pct_range` 判的是 0–100 这个口径，源端若真改成 0–1（DOC-56 的反例）判据不会自己发现，
  那是 S5 量纲判据的活；
- `captured_at_not_future` 需要注入判定时刻（`now`），否则两侧各自取钟，边界行对不上。
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from . import status_policy as sp


def _rid(row: dict, key: str, index: int) -> str:
    """按登记的主键列取编号；取不到就用行序号——宁可难看，也不许无主。"""
    v = row.get(key)
    return str(v) if v is not None else f"row-{index}"


def _as_dt(value, tz: tzinfo | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _result(problems: list[str], ids: list[str], checked: int, **extra) -> dict:
    return {"problems": problems, "ids": ids, "checked": checked, "extra": extra}


def conf_pct_range(rows: list[dict], spec: dict, key: str, now: str | None = None) -> dict:
    """红：置信度越界。边界值 0 与 100 算合格（区间闭合），100.5 与 -0.5 算越界。"""
    lo = spec.get("min")
    hi = spec.get("max")
    if lo is None or hi is None:
        raise ValueError("conf_pct_range 没登记 min/max——说不出区间就没有判据")
    problems: list[str] = []
    ids: list[str] = []
    for i, r in enumerate(rows):
        v = r.get("conf_pct")
        if v is None:
            continue  # 空值不是越界，是「缺失」——归 lot_no 那条规则的同类问题，别混
        try:
            num = float(v)
        except (TypeError, ValueError):
            rid = _rid(r, key, i)
            problems.append(f"{rid}：conf_pct 不是数字（{v!r}）")
            ids.append(rid)
            continue
        if num < float(lo) or num > float(hi):
            rid = _rid(r, key, i)
            problems.append(f"{rid}：conf_pct={v} 越界 [{lo}, {hi}]")
            ids.append(rid)
    return _result(problems, ids, len(rows))


def captured_at_not_future(rows: list[dict], spec: dict, key: str, now: str | None = None) -> dict:
    """红：采集时间晚于判定时刻。`now` 为空时用本机当前时间（调用方应尽量注入同一个字面时刻）。"""
    if not now:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    tz = None
    for r in rows:
        v = r.get("captured_at")
        if isinstance(v, datetime) and v.tzinfo:
            tz = v.tzinfo
            break
    now_dt = _as_dt(now, tz)
    if now_dt is None:
        raise ValueError(f"now 解析不了：{now!r}")
    problems: list[str] = []
    ids: list[str] = []
    for i, r in enumerate(rows):
        dt = _as_dt(r.get("captured_at"), tz)
        if dt is None:
            continue
        if dt > now_dt:
            rid = _rid(r, key, i)
            problems.append(f"{rid}：captured_at={dt.isoformat()} 晚于判定时刻 {now_dt.isoformat()}")
            ids.append(rid)
    return _result(problems, ids, len(rows), now=now_dt.isoformat())


def lot_no_blank_split(rows: list[dict], spec: dict, key: str, now: str | None = None) -> dict:
    """红：批次为空，且 NULL 与空串分开报——修复动作不同（查映射 vs 查录入）。"""
    problems: list[str] = []
    null_ids: list[str] = []
    empty_ids: list[str] = []
    for i, r in enumerate(rows):
        if "lot_no" not in r:
            continue  # 这一列不在取数结果里，是取数问题，不该记成数据脏
        v = r.get("lot_no")
        if v is None:
            rid = _rid(r, key, i)
            null_ids.append(rid)
            problems.append(f"{rid}：lot_no 是 NULL（{spec.get('split_null', '缺失')}）")
        elif str(v) == "":
            rid = _rid(r, key, i)
            empty_ids.append(rid)
            problems.append(f"{rid}：lot_no 是空串（{spec.get('split_empty', '空串')}）")
    return _result(problems, null_ids + empty_ids, len(rows),
                   null_ids=null_ids, empty_ids=empty_ids)


# id → 实现。id 必须登记在 data/mapping/field_rules.json 的 rules 里，否则 run() 抛错。
RULES: dict = {
    "conf_pct_range": conf_pct_range,
    "captured_at_not_future": captured_at_not_future,
    "lot_no_blank_split": lot_no_blank_split,
}


def registered() -> set[str]:
    return set(RULES)


def run(rule_id: str, rows: list[dict], rules_spec: dict, now: str | None = None) -> dict:
    """唯一入口：没登记就抛，不返回绿。返回里 `ran` 表示实现有没有真的被调用。"""
    spec = (rules_spec.get("rules") or {}).get(rule_id)
    if spec is None:
        raise sp.UnknownCheck(f"字段判据 {rule_id!r} 没登记在 data/mapping/field_rules.json 的 rules 里")
    fn = RULES.get(rule_id)
    if fn is None:
        # 登记了却没实现：不能返回 ok，那是把「没跑」说成「通过」
        raise sp.UnknownCheck(f"字段判据 {rule_id!r} 已登记但 app/ds/field_checks.py 里没有实现")

    key = rules_spec.get("key") or "rec_id"
    out = fn(list(rows or []), spec, key, now)
    return {
        "rule": rule_id,
        "ran": True,
        "status": "fail" if out["problems"] else "ok",
        "checked": out["checked"],
        "problems": out["problems"],
        "ids": out["ids"],
        **out.get("extra", {}),
    }
