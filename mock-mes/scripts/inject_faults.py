#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""三种故障注入 —— 每种都要能一键还原，否则它自己就是故障。

f1 结构先动、登记落后：真加了列，登记册不知道
f2 登记先动、结构没动：登记快照里多出一列，库里根本没有（判据读的就是这份快照）
f3 收紧值域、存量违约：加 CHECK，但库里早有 150 和 -10

用法：python scripts\\inject_faults.py --fault f1 --action apply|revert
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parents[1]
AGENT = HERE.parent / "fde-diagnosis-agent"

DSN = os.environ.get(
    "FDE_CLIENT_ADMIN_DSN",
    "postgresql://postgres:postgres@localhost:5432/fde_client",
)
CLIENT = "client_mes"


def audit(conn, table, op, key, after, actor="injector"):
    conn.execute(
        "insert into aud.audit_log (table_name, op, row_key, before_value, after_value, actor) "
        "values (%s, %s, %s, null, %s, %s)",
        (table, op, key, psycopg.types.json.Jsonb(after), actor),
    )


def f1(conn, action):
    sql = ("ALTER TABLE public.md_device ADD COLUMN IF NOT EXISTS vibration_grade smallint"
           if action == "apply"
           else "ALTER TABLE public.md_device DROP COLUMN IF EXISTS vibration_grade")
    conn.execute(sql)
    return sql


def f2(conn, action):
    """判据读的是 `data/mapping/source_schema/*.json` 这份导出快照，不是 gov.field_registry 表。

    所以 f2 要动的是快照：手工在登记里填一列，库里根本没有。
    还原不手改回去，而是重跑 export_registry.py 从真元数据重建——顺便证明快照是可再生的。
    """
    if action == "apply":
        p = AGENT / "data" / "mapping" / "source_schema" / "client_mes_md_device.json"
        reg = json.loads(p.read_text(encoding="utf-8"))
        if not any(c["name"] == "vibration_level" for c in reg["columns"]):
            reg["columns"].append({"name": "vibration_level"})
            p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        return "snapshot += md_device.vibration_level（库里并没有这一列）"

    r = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "scripts" / "export_registry.py")],
                       cwd=str(HERE), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("重导出快照失败：%s" % (r.stdout + r.stderr).strip())
    return "snapshot 重建（export_registry.py）"


def f3(conn, action):
    if action == "apply":
        conn.execute("ALTER TABLE public.biz_defect_record "
                     "ADD CONSTRAINT ck_conf_pct_range CHECK (conf_pct BETWEEN 0 AND 100)")
        return "CHECK (conf_pct BETWEEN 0 AND 100)"
    conn.execute("ALTER TABLE public.biz_defect_record DROP CONSTRAINT IF EXISTS ck_conf_pct_range")
    return "drop ck_conf_pct_range"


FAULTS = {"f1": f1, "f2": f2, "f3": f3}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fault", choices=sorted(FAULTS), required=True)
    ap.add_argument("--action", choices=["apply", "revert"], required=True)
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        try:
            sql = FAULTS[args.fault](conn, args.action)
            audit(conn, "public.%s" % ("md_device" if args.fault in ("f1", "f2") else "biz_defect_record"),
                  "ddl", args.fault, {"action": args.action, "sql": sql})
            conn.commit()
        except psycopg.errors.CheckViolation as exc:
            conn.rollback()
            audit_msg = str(exc).splitlines()[0]
            print("[blocked] %s %s 被 PG 拒了：%s" % (args.fault, args.action, audit_msg))
            return 3
        except psycopg.Error as exc:
            conn.rollback()
            print("[error] %s %s：%s" % (args.fault, args.action, str(exc).splitlines()[0]))
            return 2
    print("[ok] %s %s：%s" % (args.fault, args.action, sql))
    return 0


if __name__ == "__main__":
    sys.exit(main())