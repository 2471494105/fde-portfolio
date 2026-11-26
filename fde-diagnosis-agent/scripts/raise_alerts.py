#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中控台：把判据的运行台账变成事件，并把「需要人动手的那几条」推出去。

用法
----
    python -X utf8 scripts/raise_alerts.py                # 扫最近两次运行，开/关事件
    python -X utf8 scripts/raise_alerts.py --dry-run      # 只打印将要发生什么，不写库
    python -X utf8 scripts/raise_alerts.py --list         # 看还开着的事件
    python -X utf8 scripts/raise_alerts.py --ack=3 --by=张工
    python -X utf8 scripts/raise_alerts.py --close=3 --reason="复跑已归零，2026-12-01 验收"
    python -X utf8 scripts/raise_alerts.py --webhook=https://... --channel=feishu

为什么「推送」必须从这个脚本走，而不是让判据自己发消息
--------------------------------------------------
判据的职责是判定，发消息是运营动作。混在一起会有两个后果：判据跑一次就推一次
（刷屏），以及判据脚本要持有外部凭证（凭据进了代码库）。这里只做搬运：
判据 → 台账 → 事件 → 通道，任何一环失败都能单独重跑。

退出码：0 无事 / 1 有新增需要处理 / 2 自身出错（不能当「今天没事」）
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import alerts as al  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"


def load_rows(dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, check_id, ran_at, status, red_count, details"
            " FROM gov_check_run ORDER BY ran_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]


def load_orders(dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM gov_remediation_order ORDER BY rm_no")
        return [dict(r) for r in cur.fetchall()]


def as_run(row: dict) -> dict:
    d = row.get("details") or {}
    return {
        "run_id": row["run_id"],
        "ran_at": row["ran_at"],
        "status": row["status"],
        "red_count": row["red_count"],
        # 没有 ids 键就留 None：老台账只存了条数，不能当成「空集合」参与比对
        "ids": d["ids"] if "ids" in d else None,
        "error": d.get("error"),
    }


def plan(rows: list[dict], orders: list[dict], now: datetime) -> tuple[list[dict], list[dict], dict]:
    """纯函数部分全在 app/ds/alerts.py；这里只负责把库里的形状喂进去。"""
    by_check: dict[str, list[dict]] = {}
    for r in rows:
        by_check.setdefault(r["check_id"], []).append(r)

    events: list[dict] = []
    closes: list[dict] = []
    for check_id in al.TITLES:
        runs = by_check.get(check_id) or []
        if not runs:
            continue
        cur = as_run(runs[0])
        prev = as_run(runs[1]) if len(runs) > 1 else None
        out = al.plan_transition(check_id, prev, cur, owner=al.OWNERS.get(check_id))
        events.extend(out["events"])
        closes.extend(out["closes"])

    last = {cid: as_run(rs[0]) for cid, rs in by_check.items() if rs}
    # 只对「已经跑过的判据」查静默：从没跑过的判据属于上线问题，不该混进数据问题里
    events.extend(al.silent_events(last, now, expected=list(last.keys()) or []))
    events.extend(al.overdue_events(orders, now.date()))

    # 同一个 dedup_key 一次只能出一条，去重放在最后一道
    seen: dict[str, dict] = {}
    for e in events:
        if e["dedup_key"] in seen:
            continue
        seen[e["dedup_key"]] = e
    return list(seen.values()), closes, al.summarize([{"state": "open", **e} for e in events])


def upsert(dsn: str, events: list[dict], closes: list[dict], now: datetime) -> dict:
    """开/更新事件，关掉已归零的事件。返回本次真正推出去的条数。

    更新也涨 seen_count：它回答的是「这件事存在了几轮」，不是「推了几次」。
    """
    import psycopg
    from psycopg.rows import dict_row

    pushed: list[dict] = []
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        for e in events:
            cur.execute(
                "SELECT alert_id, state, seen_count FROM gov_alert"
                " WHERE dedup_key = %s AND state <> 'closed'",
                (e["dedup_key"],),
            )
            hit = cur.fetchone()
            payload = json.dumps(e.get("detail") or {}, ensure_ascii=False, default=str)
            if hit:
                cur.execute(
                    "UPDATE gov_alert SET last_seen = %s, seen_count = seen_count + 1,"
                    " title = %s, severity = %s, detail = %s::jsonb"
                    " WHERE alert_id = %s",
                    (now, e["title"], e["severity"], payload, hit["alert_id"]),
                )
                alert_id = hit["alert_id"]
                is_new = False
            else:
                cur.execute(
                    "INSERT INTO gov_alert (dedup_key, check_id, kind, severity, title, detail, owner, first_seen, last_seen)"
                    " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) RETURNING alert_id",
                    (
                        e["dedup_key"], e["check_id"], e["kind"], e["severity"],
                        e["title"], payload, e.get("owner"), now, now,
                    ),
                )
                alert_id = cur.fetchone()["alert_id"]
                is_new = True
            # 只有两类值得叫醒人：第一次出现，或红行里出现了新增编号。
            # 「同一批红再跑一次」只涨 seen_count，不推——刷屏等于没有告警。
            new_ids = (e.get("detail") or {}).get("new_ids") or []
            if e.get("notify") and (is_new or new_ids):
                pushed.append({**e, "alert_id": alert_id, "is_new": is_new})
        for c in closes:
            cur.execute(
                "UPDATE gov_alert SET state = 'closed', closed_at = %s, closed_reason = %s"
                " WHERE dedup_key = %s AND state <> 'closed'",
                (now, c["reason"], c["dedup_key"]),
            )
    return {"pushed": pushed}


def deliver(pushed: list[dict], webhook: str, channel: str, dsn: str) -> list[str]:
    """外发。没配 webhook 就不外发，但打印出来——「没通道」不能变成「没发生」。"""
    errs: list[str] = []
    if not webhook:
        if pushed:
            print(f"（未配置外部通道，{len(pushed)} 条已落投递箱）")
        return errs

    def body(items: list[dict]) -> dict:
        lines = [f"[{i+1}] {e['title']}（{e['severity']} · {e.get('owner')}）" for i, e in enumerate(items)]
        text = "数据治理中控台\n" + "\n".join(lines)
        if channel == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if channel == "dingtalk":
            return {"text": {"content": text}}
        return {"text": text, "items": items}

    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(body(pushed), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"HTTP {resp.status}")
        print(f"已外发 {len(pushed)} 条 → {channel}")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        errs.append(err)
        print(f"[warn] 外发失败（事件仍在投递箱里，不算推过）：{err}", file=sys.stderr)
        _mark_delivery(dsn, pushed, channel, err)
    return errs


def _mark_delivery(dsn: str, pushed: list[dict], channel: str, err: str) -> None:
    """外发失败要留在事件上：否则第二天没人知道这条其实没送达。"""
    import psycopg

    note = json.dumps({"channel": channel, "ok": False, "error": err}, ensure_ascii=False)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for e in pushed:
            cur.execute(
                "UPDATE gov_alert SET detail = detail || jsonb_build_object('delivery', %s::jsonb)"
                " WHERE alert_id = %s",
                (note, e["alert_id"]),
            )


def cmd_list(dsn: str) -> int:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT alert_id, severity, state, check_id, title, owner, first_seen, last_seen, seen_count"
            " FROM gov_alert WHERE state <> 'closed' ORDER BY severity DESC, last_seen DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        print("没有未关闭的事件")
        return 0
    for r in rows:
        print(
            f"#{r['alert_id']:<3} [{r['severity']:5s}] {r['state']:5s} {r['seen_count']}轮 "
            f"{r['title']}（{r['owner'] or '待指派'}，最近 {r['last_seen']:%m-%d %H:%M}）"
        )
    return 0


def cmd_state(dsn: str, alert_id: int, state: str, by: str | None, reason: str | None, now: datetime) -> int:
    import psycopg

    if state == "acked" and not by:
        print("[error] 认领要写 who：没有责任人的 ack 等于没认领", file=sys.stderr)
        return 2
    if state == "closed" and not reason:
        print("[error] 关闭要写原因；且不能写「已修复」，修复由复跑归零证明", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        if state == "acked":
            cur.execute(
                "UPDATE gov_alert SET state = 'acked', acked_by = %s, acked_at = %s"
                " WHERE alert_id = %s AND state = 'open'",
                (by, now, alert_id),
            )
        else:
            cur.execute(
                "UPDATE gov_alert SET state = 'closed', closed_at = %s, closed_reason = %s"
                " WHERE alert_id = %s AND state <> 'closed'",
                (now, reason, alert_id),
            )
        got = cur.rowcount
    if not got:
        print(f"[warn] #{alert_id} 不存在或状态已不是预期（没有改动）", file=sys.stderr)
        return 2
    print(f"#{alert_id} → {state}")
    return 0


def main(argv: list[str]) -> int:
    dsn = os.environ.get("FDE_CLIENT_DSN", CLIENT_DSN)
    webhook = os.environ.get("FDE_ALERT_WEBHOOK", "")
    channel = os.environ.get("FDE_ALERT_CHANNEL", "json")
    dry = False
    mode = "scan"
    alert_id = None
    by = reason = None
    now = datetime.now(timezone.utc).astimezone()

    for a in argv:
        if a == "--dry-run":
            dry = True
        elif a == "--list":
            mode = "list"
        elif a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--webhook="):
            webhook = a.split("=", 1)[1]
        elif a.startswith("--channel="):
            channel = a.split("=", 1)[1]
        elif a.startswith("--ack="):
            mode, alert_id = "acked", int(a.split("=", 1)[1])
        elif a.startswith("--close="):
            mode, alert_id = "closed", int(a.split("=", 1)[1])
        elif a.startswith("--by="):
            by = a.split("=", 1)[1]
        elif a.startswith("--reason="):
            reason = a.split("=", 1)[1]
        elif a.startswith("--now="):
            now = datetime.fromisoformat(a.split("=", 1)[1]).astimezone()
        elif a == "--no-notify":
            webhook = ""
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    if mode == "list":
        return cmd_list(dsn)
    if mode in ("acked", "closed"):
        return cmd_state(dsn, alert_id, mode, by, reason, now)

    rows = load_rows(dsn)
    events, closes, summary = plan(rows, load_orders(dsn), now)
    print(f"判据运行 {len(rows)} 条 · 事件 {len(events)} 条（红 {summary['red']} / 黄 {summary['amber']}）· 待关 {len(closes)} 条")
    for e in events:
        flag = "推" if e.get("notify") else "记"
        print(f"[{flag}] [{e['severity']:5s}] {e['title']}（{e.get('owner')}）")
    for c in closes:
        print(f"[关] {c['dedup_key']}：{c['reason']}")

    if dry:
        return 0

    res = upsert(dsn, events, closes, now)
    errs = deliver(res["pushed"], webhook, channel, dsn)
    if errs:
        return 2
    return 1 if res["pushed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("中控台脚本自身崩了（这一轮不能当「今天没事」）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
