#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""问题事件 → 目标数据：把一条事件的编号翻成「那张表里到底哪几行、值长什么样」。

为什么单独一个模块
------------------
中控台说的是「40 行没有闭环」，客户下一句一定是「哪 40 行？长什么样？我改哪一栏？」
这三问答不上，事件就只是噪音。这个模块只做一次翻译：事件 → 台账里的编号 →
目标表的行，不重算判据。

三条口径
1. **只按编号回查，不重算判据**。判据说 5 行，这里就只查这 5 行。页面与判据各自
   判一遍 = 两套口径，客户对不上账时不知道该信谁。
2. **编号 → 目标表的映射读 `gov_issue_catalog.targets`**（Python 源头在 fde 侧的
   `app/ds/issues.py`）。这里不硬编码表名：认不得的类别就老实说「未登记目标表」。
3. **空值照原样返回 None / ''**，页面负责显示「空」。不补默认值、不填 0——
   把空写成 0 是这批脏数据最典型的一种谎。
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

try:  # uvicorn api.app:app
    from .rules import status_of
except ImportError:  # 直接 python api/app.py 的旧用法
    from rules import status_of  # type: ignore

# 表名 → 客户看得懂的名字（页面上写「工单」，不写 biz_work_order）
TABLE_LABELS = {
    "biz_work_order": "工单",
    "biz_defect_record": "缺陷记录",
    "biz_alarm": "报警",
}


def _keys_of(ids: list[str], target: dict) -> list[str]:
    """把编号还原成定位键：`prefix#key` 砍掉前缀，裸编号（缺陷判据）直接当键。"""
    prefixes = target.get("prefixes") or []
    out: list[str] = []
    for raw in ids:
        s = str(raw)
        hit = None
        for p in prefixes:
            if p and s.startswith(p):
                hit = p
                break
        if hit:
            out.append(s[len(hit):])
        elif "#" in s:
            continue  # 有别的前缀（例如工单）——不归这张表，别硬塞
        elif target.get("bare"):
            out.append(s)
    return sorted(set(out), key=lambda s: (len(s), s))


def _dump(v):
    from datetime import date, datetime

    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return v


def problem(dsn: str, alert_id: int, size: int = 50, page: int = 1) -> dict | None:
    size = max(1, min(size, 200))
    page = max(1, page)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        alert = conn.execute(
            "select alert_id, dedup_key, check_id, severity, title, state, owner, detail,"
            " first_seen, last_seen, seen_count, closed_reason, closed_at"
            " from gov_alert where alert_id = %s", (alert_id,)).fetchone()
        if not alert:
            return None

        rule = status_of(alert["check_id"])
        detail = alert["detail"] or {}
        issue_id = detail.get("issue")
        cat = conn.execute(
            "select issue_id, short, name, check_id, owner, scope, first_step, evidence, note, targets"
            " from gov_issue_catalog where issue_id = %s",
            (issue_id,)).fetchone() if issue_id else None
        if cat is None:  # 老事件没带 issue 字段：按判据反查，别猜
            cat = conn.execute(
                "select issue_id, short, name, check_id, owner, scope, first_step, evidence, note, targets"
                " from gov_issue_catalog where check_id = %s", (alert["check_id"],)).fetchone()

        # 编号只在台账里：事件 detail 存的是 run_id 与统计数，行编号在 gov_check_run.details
        ids: list[str] = []
        run_id = detail.get("run_id")
        run = None
        if run_id:
            run = conn.execute(
                "select run_id, ran_at, dataset, details from gov_check_run where run_id = %s",
                (run_id,)).fetchone()
            if run:
                ids = list((run["details"] or {}).get("ids") or [])

        groups = []
        for t in (cat["targets"] if cat else []) or []:
            table = t.get("table")
            cols = list(t.get("cols") or [])
            key = t.get("key")
            if not (table and key and cols):
                continue
            keys = _keys_of(ids, t)
            rows, total = [], len(keys)
            if keys:
                off = (page - 1) * size
                rows = conn.execute(
                    "select %s from %s where %s::text = any(%%s::text[])"
                    " order by %s limit %%s offset %%s" % (", ".join(cols), table, key, key),
                    (keys, size, off)).fetchall()
            groups.append({
                "table": table,
                "label": TABLE_LABELS.get(table, table),
                "key": key,
                "cols": cols,
                "fix": t.get("fix") or "",
                "collection": t.get("collection") or "",
                "page_url": t.get("page") or "",
                "matched": total,
                "shown": len(rows),
                "rows": [{k: _dump(v) for k, v in r.items()} for r in rows],
            })

    return {
        "alert": {
            "alert_id": alert["alert_id"],
            "dedup_key": alert["dedup_key"],
            "check_id": alert["check_id"],
            "severity": alert["severity"],
            "title": alert["title"],
            "state": alert["state"],
            "owner": alert["owner"],
            "first_seen": _dump(alert["first_seen"]),
            "last_seen": _dump(alert["last_seen"]),
            "seen_count": alert["seen_count"],
            "closed_reason": alert["closed_reason"],
            "closed_at": _dump(alert["closed_at"]),
            "issue": detail.get("issue"),
            "issue_short": detail.get("issue_short"),
            "issue_name": detail.get("issue_name"),
            "first_step": detail.get("first_step"),
            "red_count": detail.get("red_count"),
            "new_ids": detail.get("new_ids") or [],
            "gone_ids": detail.get("gone_ids") or [],
        },
        "issue": {
            "issue_id": cat["issue_id"] if cat else None,
            "short": cat["short"] if cat else None,
            "name": cat["name"] if cat else None,
            "scope": cat["scope"] if cat else None,
            "evidence": cat["evidence"] if cat else None,
            "note": cat["note"] if cat else None,
            "first_step": cat["first_step"] if cat else None,
        },
        "run": {"run_id": run["run_id"], "ran_at": _dump(run["ran_at"]),
                "dataset": run["dataset"]} if run else None,
        # 这一类的规矩是谁定的：assumed 的必须写在页面顶上，否则客户会把这 362 行
        # 当成「你们判错了」，而事实是「在等你回答工位口径那一题」。
        "rule": {
            "status": rule.get("status"),
            "basis": rule.get("basis"),
            "effect": rule.get("effect"),
            "source": rule.get("source"),
            "counts_in_total": bool(rule.get("counts_in_total")),
        },
        "ids_total": len(ids),
        "groups": groups,
        "page": page,
        "size": size,
        "hint": "行数 = 判据台账里这轮的编号数；表格里只显示本页。空值就是库里空着，不是这里漏了。",
    }
