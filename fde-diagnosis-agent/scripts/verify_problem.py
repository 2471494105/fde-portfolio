#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收「问题数据 → 目标数据」这条链路（T27）。

验收的对象不是代码跑没跑通，而是四句承诺：
1. 每条**客户库**事件都能点进明细页，并且明细页的行数等于判据台账里的编号数
   （不是「大概那么多」，是数得出来的那个数）；
2. 翻出来的行就是判据当时判的那几行——抽一条脏值核对原文（red 5 的 conf_pct=150）；
3. 每类都有「改哪一栏」的指引，且指引里的集合名真的能在 /v1 上取到数；
4. 外部自检（沙箱库 / 源端样本）**不在客户事件流里**——上一轮那条口径错误要能
   被机器查出来，而不是靠人记得。

用法：
    python scripts/verify_problem.py            # 需要 8021 在跑
    python scripts/verify_problem.py --base=http://127.0.0.1:8021
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.request

import psycopg
from psycopg.rows import dict_row

DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
BASE = "http://127.0.0.1:8021"
EXTERNAL = ("data_contract", "source_values", "source_schema")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def alive_alerts() -> list[dict]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return [dict(r) for r in conn.execute(
            "select alert_id, check_id, severity, state, detail from gov_alert"
            " where state <> 'closed' order by alert_id").fetchall()]


def ids_of(run_id: int | None) -> list[str]:
    if not run_id:
        return []
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        row = conn.execute("select details from gov_check_run where run_id = %s",
                           (run_id,)).fetchone()
    return list((row["details"] or {}).get("ids") or []) if row else []


def main(argv: list[str]) -> int:
    global BASE
    for a in argv:
        if a.startswith("--base="):
            BASE = a.split("=", 1)[1].rstrip("/")

    fails: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('：' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    alerts = alive_alerts()
    by_check = {a["check_id"]: a for a in alerts}

    # P0 外部自检不进客户事件流（含反例：真出现过才叫验证）
    ext_alive = [c for c in EXTERNAL if c in by_check]
    check(not ext_alive, "P0 外部自检不在客户事件流里",
          "外部判据还有活事件：" + ", ".join(ext_alive) if ext_alive else f"活事件 {len(alerts)} 条")

    # P1 三条客户事件：接口能开、行数对得上台账
    for cid in ("station_contract", "defect_fields", "alarm_ack"):
        a = by_check.get(cid)
        if not a:
            check(False, f"P1 {cid} 有活事件", "没有活事件，没法验明细链")
            continue
        p = get(f"/v1/problem/{a['alert_id']}")
        want = len(ids_of((a["detail"] or {}).get("run_id")))
        got = sum(g["matched"] for g in p["groups"])
        check(want == got and got > 0, f"P1 {cid} 行数=台账编号数",
              f"台账 {want} 个编号·明细页 {got} 行")

    # P2 翻出来的是判据当时那几行（抽 red 5 的 conf_pct=150、red 4 的未来时间）
    a2 = by_check.get("defect_fields")
    if a2:
        p = get(f"/v1/problem/{a2['alert_id']}")
        rows = {int(r["rec_id"]): r for r in p["groups"][0]["rows"]}
        ok = (rows.get(5, {}).get("conf_pct") == 150 and rows.get(6, {}).get("conf_pct") == -10
              and str(rows.get(4, {}).get("captured_at") or "").startswith("2027-01-01")
              and (rows.get(8, {}).get("lot_no") in (None, "")))
        check(ok, "P2 抽样对账：150 / -10 / 2027-01-01 / 空批次", f"取到 {len(rows)} 行")

    # P3 指引：每类都有 fix，且指引里的集合真的取得到数
    a1 = by_check.get("station_contract")
    a3 = by_check.get("alarm_ack")
    if a1:
        p = get(f"/v1/problem/{a1['alert_id']}")
        gs = p["groups"]
        check(len(gs) == 2 and all(g["fix"] for g in gs), "P3a 归属类两张表都有改哪一栏",
              " / ".join(f"{g['table']}:{g['matched']}" for g in gs))
        wq = get("/v1/work_orders?q=WO-2026-0001&size=5")
        check(wq["total"] == 1 and wq["items"][0]["wo_no"] == "WO-2026-0001",
              "P3b 工单跳转链接能取到那一单")
    if a3:
        p = get(f"/v1/problem/{a3['alert_id']}")
        r0 = p["groups"][0]["rows"][0]
        check(p["groups"][0]["fix"] != "" and p["groups"][0]["matched"] == 40,
              "P3c 报警类 40 行 + 改哪一栏", f"首行 alarm_id={r0['alarm_id']}")
        aq = get("/v1/alarms?q=CX-101&size=5")
        check(aq["total"] >= 1, "P3d 报警跳转链接能取到那一批", f"total={aq['total']}")

    # P4 明细页本身：能看到「空」的原样，而不是被填成 0
    if a3:
        p = get(f"/v1/problem/{a3['alert_id']}")
        blanks = sum(1 for r in p["groups"][0]["rows"]
                     if r.get("acked_by") in (None, "") and r.get("cleared_at") in (None, ""))
        check(blanks == 40, "P4 两列都空的行照原样返回（没被补值）", f"{blanks} 行")

    # P5 不存在的编号要 404，不能给一张空表让人以为「没问题」
    try:
        get("/v1/problem/999999")
        check(False, "P5 未知事件返回 404")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "P5 未知事件返回 404", f"HTTP {e.code}")


    # P6 处置清单导出：行数 = 台账编号数、列齐、导出不改数据、未知事件 404
    if a1 and a2 and a3:
        import csv
        import io

        def export(aid: int) -> tuple[int, list[str]]:
            raw = urllib.request.urlopen(f"{BASE}/v1/problem/{aid}/export.csv", timeout=30).read()
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
            return len(rows) - 1, rows[0]

        n1, head1 = export(a1["alert_id"])
        n2, _ = export(a2["alert_id"])
        n3, _ = export(a3["alert_id"])
        want = (len(ids_of((a1["detail"] or {}).get("run_id")))
                + len(ids_of((a2["detail"] or {}).get("run_id")))
                + len(ids_of((a3["detail"] or {}).get("run_id"))))
        check(n1 + n2 + n3 == want, "P6a 导出总行数=三条事件的台账编号数",
              f"{n1}+{n2}+{n3}={n1 + n2 + n3} · 台账 {want}")
        check(len(head1) == 14 and "改哪一栏" in head1 and "先做这一步" in head1,
              "P6b 表头含「先做这一步」「改哪一栏」", f"{len(head1)} 列")

        # 导出是只读动作：导完再数一遍，报警两列还是一样的 40 条空
        p9 = get(f"/v1/problem/{a3['alert_id']}")
        blanks9 = sum(1 for r in p9["groups"][0]["rows"]
                      if r.get("acked_by") in (None, "") and r.get("cleared_at") in (None, ""))
        check(blanks9 == 40, "P6c 导出没有顺手改数据（两列仍 40 条空）", f"{blanks9} 行")

        try:
            urllib.request.urlopen(f"{BASE}/v1/problem/999999/export.csv", timeout=15)
            check(False, "P6d 未知事件导出返回 404")
        except urllib.error.HTTPError as e:
            check(e.code == 404, "P6d 未知事件导出返回 404", f"HTTP {e.code}")

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 1
    print("[OK] T27 明细链全部通过（事件 → 台账编号 → 目标行 → 指引）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
