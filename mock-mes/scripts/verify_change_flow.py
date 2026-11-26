#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T8 验收：把故障灌进去，看判据是不是真的会喊。

十五项检查。跑法：python scripts\verify_change_flow.py
每项都自带还原；中途崩了直接重跑，注入全是幂等的。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parents[1]
AGENT = HERE.parent / "fde-diagnosis-agent"
DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
CHECK = AGENT / "scripts" / "check_source_schema.py"

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def run(script, *args):
    return subprocess.run([sys.executable, "-X", "utf8", str(script), *args], cwd=str(HERE),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def judge():
    r = subprocess.run([sys.executable, "-X", "utf8", str(CHECK),
                        "--client=client_mes", "--dsn=%s" % DSN], cwd=str(AGENT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def refresh():
    return run(HERE / "scripts" / "export_registry.py").returncode


def q(sql, *params):
    with psycopg.connect(DSN) as c:
        return c.execute(sql, params).fetchone()


def preclean():
    """把库和快照先弄回基线：上次跑到一半崩了，重跑也能自愈。"""
    for f in ("f1", "f2", "f3"):
        run(HERE / "scripts" / "inject_faults.py", "--fault", f, "--action", "revert")
    run(HERE / "scripts" / "simulate_change.py", "--req", "CR-2026-0001", "--action", "revert")
    return refresh()


def main() -> int:
    preclean()
    rc, out = judge()
    check("0 基线干净", rc == 0, "判据 exit=%d" % rc)

    # ---- f1 结构先动、登记落后 ----
    check("1 f1 注入", run(HERE / "scripts" / "inject_faults.py", "--fault", "f1", "--action", "apply").returncode == 0)
    rc, out = judge()
    check("2 判据报 S1 新增列", rc != 0 and "vibration_grade" in out, "exit=%d" % rc)
    check("3 f1 还原后转绿", run(HERE / "scripts" / "inject_faults.py", "--fault", "f1", "--action", "revert").returncode == 0 and judge()[0] == 0)

    # ---- f2 登记先动、结构没动 ----
    check("4 f2 注入（改的是判据读的那份快照）", run(HERE / "scripts" / "inject_faults.py", "--fault", "f2", "--action", "apply").returncode == 0)
    rc, out = judge()
    check("5 判据报「登记了却没观测到」", rc != 0 and "vibration_level" in out, "exit=%d" % rc)
    check("6 f2 还原（重导出快照）后转绿", run(HERE / "scripts" / "inject_faults.py", "--fault", "f2", "--action", "revert").returncode == 0 and judge()[0] == 0)

    # ---- f3 收紧值域、存量违约 ----
    r = run(HERE / "scripts" / "inject_faults.py", "--fault", "f3", "--action", "apply")
    msg = (r.stdout + r.stderr).strip()
    check("7 存量违约让 DDL 直接失败", r.returncode == 3 and ("约束" in msg or "constraint" in msg.lower()),
          msg.splitlines()[-1] if msg else "")
    check("8 f3 还原", run(HERE / "scripts" / "inject_faults.py", "--fault", "f3", "--action", "revert").returncode == 0)

    # ---- 没有审批的单不许执行 ----
    r = run(HERE / "scripts" / "simulate_change.py", "--req", "CR-2026-0002", "--action", "apply")
    denied = q("select count(*) from aud.audit_log where op='denied' and row_key='CR-2026-0002'")[0]
    check("9 pending 单被拒且留痕", r.returncode == 1 and denied >= 1, (r.stdout + r.stderr).strip())

    # ---- approved 单执行；三处不同步会立刻体现在判据上 ----
    ok_apply = run(HERE / "scripts" / "simulate_change.py", "--req", "CR-2026-0001", "--action", "apply").returncode == 0
    has_col = q("select 1 from information_schema.columns where table_schema='public' "
                "and table_name='md_device' and column_name='torque_limit'") is not None
    has_reg = q("select 1 from gov.field_registry where client='client_mes' "
                "and table_name='md_device' and column_name='torque_limit'") is not None
    check("10 结构 + 登记册都动了", ok_apply and has_col and has_reg)

    rc, out = judge()
    check("11 快照没刷新，判据仍然红", rc != 0 and "torque_limit" in out, "exit=%d" % rc)
    check("12 刷新快照后转绿", refresh() == 0 and judge()[0] == 0)

    ok_revert = run(HERE / "scripts" / "simulate_change.py", "--req", "CR-2026-0001", "--action", "revert").returncode == 0
    gone = q("select 1 from information_schema.columns where table_schema='public' "
             "and table_name='md_device' and column_name='torque_limit'") is None
    reg_gone = q("select 1 from gov.field_registry where client='client_mes' "
                 "and table_name='md_device' and column_name='torque_limit'") is None
    check("13 回滚把列和登记都撤了", ok_revert and gone and reg_gone)

    rc, out = judge()
    check("14 快照仍停在旧版本，判据报消失列", rc != 0 and "torque_limit" in out, "exit=%d" % rc)
    check("15 再刷新后回到基线", refresh() == 0 and judge()[0] == 0)

    wo = q("select count(*) from biz_work_order")[0]
    dr = q("select count(*) from biz_defect_record")[0]
    reg = q("select count(*) from gov.field_registry")[0]
    print()
    print("末态：工单 %d / 缺陷记录 %d / 登记册 %d 行" % (wo, dr, reg))
    print("结果：%d 项检查，%d 项失败" % (16, len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())