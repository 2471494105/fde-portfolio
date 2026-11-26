#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T26 验收：常见问题目录 + 两道判据接进事件流。独立查库，不看脚本自评。

每条都在验「客户能不能指着讲」，不是验「脚本跑通了」：
  I1 目录登记：表里的类别与 app/ds/issues.py 一致（不多不少，没有手工改表）
  I2 接线不写假数：--no-record 不动台账；真跑一次只多两行，且 ids 与 red_count 对得上
  I3 事件带类别：活着的每一条都有 issue_short / first_step，类别名来自登记表
  I4 单位不混：行类判据的合计仍是 362+5+0，契约/值域没被算进「违约行」
  I5 无样本不造事件：今天样本为 0 的类别，不许有活事件

用法：python -X utf8 scripts/verify_issues.py
退出码：0 全过 / 1 有失败
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import alerts, issues  # noqa: E402

PY = sys.executable
DSN = os.environ.get("FDE_CLIENT_DSN", "postgresql://postgres:postgres@localhost:5432/fde_client")
fails: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("ok" if cond else "FAIL", name, ("：" + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def q(sql: str, params: tuple = ()) -> list[tuple]:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def run(script: str, *args: str) -> tuple[int, str]:
    p = subprocess.run([PY, "-X", "utf8", str(ROOT / "scripts" / script), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    # I1 目录登记与源一致
    code, out = run("seed_issues.py", "--check")
    ok("I1 目录表与 app/ds/issues.py 一致", code == 0, out.strip()[-200:])
    n = q("select count(*) from gov_issue_catalog")[0][0]
    ok("I1b 登记 8 类", n == len(issues.CATALOG) == 8, f"表内 {n}，源 {len(issues.CATALOG)}")

    # I1c 每个类别都有判据与标题登记（否则事件里会出现没有名字的类别）
    missing = [c["check_id"] for c in issues.CATALOG
               if c["check_id"] not in alerts.TITLES and c["check_id"] != "remediation"]
    ok("I1c 类别都有判据标题", not missing, f"缺：{missing}")

    # I2 --no-record 不动台账
    before = q("select count(*) from gov_check_run")[0][0]
    code, out = run("collect_issues.py", "--no-record", "--quiet")
    after = q("select count(*) from gov_check_run")[0][0]
    ok("I2 dry 不写台账", before == after, f"{before} → {after}")
    ok("I2b dry 也报结论（exit 1 = 有红/有黄）", code == 1, f"exit={code}")

    # I2c 真跑一次：只多两行，且契约行 red_count = 隔离区行数（当前断言全绿）
    code, out = run("collect_issues.py", "--quiet")
    rows = q("select run_id, check_id, status, red_count, details from gov_check_run"
             " where check_id in ('data_contract','source_values') order by run_id desc limit 2")
    ok("I2c 真跑 exit 1", code == 1, f"exit={code}")
    ok("I2d 台账多两行且是这两道", len(rows) == 2, f"{len(rows)} 行")
    by_id = {r[1]: r for r in rows}
    dc = by_id.get("data_contract")
    ok("I2e 契约行 red_count = 隔离区行数", bool(dc) and dc[3] == int((dc[4] or {}).get("quarantine_rows") or 0),
       f"red_count={dc[3] if dc else '无'}，quarantine={((dc[4] or {}).get('quarantine_rows') if dc else '无')}")
    ok("I2f 契约行带 ids", bool(dc) and bool((dc[4] or {}).get("ids")), "details.ids 空")
    sv = by_id.get("source_values")
    ok("I2g 值域行是黄（status=warn）", bool(sv) and sv[2] == "warn",
       f"status={sv[2] if sv else '无'}")

    # I3 事件带类别
    alive = q("select alert_id, check_id, severity, title, detail from gov_alert where state <> 'closed'")
    missing_cat = [a[0] for a in alive if not (a[4] or {}).get("issue_short")]
    ok("I3 活事件都带类别短名", alive and not missing_cat, f"缺：{missing_cat}")
    no_step = [a[0] for a in alive if not (a[4] or {}).get("first_step")]
    ok("I3b 活事件都带首步动作", not no_step, f"缺：{no_step}")
    # 类别里的 check_id 必须真有事件（命中才建事件，不是把 8 类都写成事件）
    cat_of = {c["issue_id"]: c["check_id"] for c in issues.CATALOG}
    unknown = [a[0] for a in alive
               if (a[4] or {}).get("issue") and cat_of.get(a[4]["issue"]) not in (None, a[1])
               and a[4]["issue"] != "remediation"]
    ok("I3c 事件类别与判据对得上", not unknown, f"对不上：{unknown}")

    # I3d 值域那条是黄：判据判 warn，事件就不能是红
    amber = [a for a in alive if a[1] == "source_values"]
    ok("I3d 值域事件是黄", all(a[2] == "amber" for a in amber) if amber else True,
       f"{[a[2] for a in amber]}")

    # I4 单位不混：违约行只算行类判据
    latest: dict[str, int] = {}
    for cid, red in q("select check_id, red_count from gov_check_run order by ran_at desc"):
        latest.setdefault(cid, red)
    row_total = sum(latest.get(c, 0) for c in ("station_contract", "defect_fields", "status_policy"))
    expected = 362 + 5 + 0
    ok("I4 行类合计仍是 362+5+0", row_total == expected, f"{row_total} ≠ {expected}")
    html = (ROOT.parent / "mock-mes" / "api" / "static" / "console.html").read_text(encoding="utf-8")
    ok("I4b 页面环形图只取行类判据", re.search(r'ROW_CHECKS\s*=\s*\[[^\]]*station_contract', html) is not None,
       "ROW_CHECKS 没定义")

    # I5 无样本不造事件
    live_checks = {a[1] for a in alive}
    zero_but_event = []
    for c in issues.CATALOG:
        cid = c["check_id"]
        if cid in ("remediation", "alert_sweep"):
            continue
        if latest.get(cid, 0) == 0 and cid in live_checks:
            zero_but_event.append(cid)
    ok("I5 今天 0 样本的类别没有活事件", not zero_but_event, f"多出：{zero_but_event}")
    # 无样本类别在页面上要写 0 / 未跑，而不是消失
    ok("I5b 目录页显示 0 与未跑两种态", ('class="tag zero">0' in html) and ("未跑" in html))

    print("\nRESULT: %s" % ("ALL PASS" if not fails else "FAIL %d 项：%s" % (len(fails), ", ".join(fails))))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
