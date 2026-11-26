#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""按变更单真的改库结构 —— 只认 approved 的单。

这个脚本存在的意义不是"会执行 DDL"，是"执行前会看单子是谁批的"。
没有审批的改动，在客户现场也是有人忍不住偷偷改的；模拟器要把这条路径堵死：
  pending 的单 → 拒绝 + 落一条 denied 审计，exit 1。

用法：
    python scripts\simulate_change.py --req CR-2026-0001 --action apply
    python scripts\simulate_change.py --req CR-2026-0001 --action revert
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

DSN = os.environ.get(
    "FDE_CLIENT_ADMIN_DSN",
    "postgresql://postgres:postgres@localhost:5432/fde_client",
)
CLIENT = "client_mes"


def audit(conn, table, op, key, before, after, actor="postgres"):
    conn.execute(
        "insert into aud.audit_log (table_name, op, row_key, before_value, after_value, actor) "
        "values (%s, %s, %s, %s, %s, %s)",
        (table, op, key, psycopg.types.json.Jsonb(before) if before else None,
         psycopg.types.json.Jsonb(after) if after else None, actor),
    )


def next_ordinal(conn, table):
    return conn.execute(
        "select coalesce(max(ordinal), 0) + 1 from gov.field_registry where client=%s and table_name=%s",
        (CLIENT, table),
    ).fetchone()[0]


def apply_add_column(conn, req):
    table, column = req["table_name"], req["column_name"]
    payload = req["payload"] or {}
    sql = "ALTER TABLE public.%s ADD COLUMN IF NOT EXISTS %s %s" % (
        table, column, payload.get("type", "numeric"))
    conn.execute(sql)
    if not conn.execute(
        "select 1 from gov.field_registry where client=%s and table_name=%s and column_name=%s",
        (CLIENT, table, column),
    ).fetchone():
        conn.execute(
            "insert into gov.field_registry (client, table_name, column_name, ordinal, data_type, "
            "is_nullable, is_key, observed_via, unit, registered_by) "
            "values (%s, %s, %s, %s, %s, %s, false, 'change_request', %s, 'simulator')",
            (CLIENT, table, column, next_ordinal(conn, table), payload.get("type"),
             bool(payload.get("nullable", True)), payload.get("unit")),
        )
    return sql


def revert_add_column(conn, req):
    table, column = req["table_name"], req["column_name"]
    sql = "ALTER TABLE public.%s DROP COLUMN IF EXISTS %s" % (table, column)
    conn.execute(sql)
    conn.execute(
        "delete from gov.field_registry where client=%s and table_name=%s and column_name=%s "
        "and observed_via='change_request'", (CLIENT, table, column))
    return sql


HANDLERS = {
    "add_column": (apply_add_column, revert_add_column),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--req", required=True)
    ap.add_argument("--action", choices=["apply", "revert"], required=True)
    args = ap.parse_args()

    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("select req_no, table_name, change_kind, column_name, payload, status "
                        "from gov.change_request where req_no=%s", (args.req,))
            row = cur.fetchone()

        if not row:
            print("没有这张变更单：%s" % args.req)
            return 2
        req = dict(zip(("req_no", "table_name", "change_kind", "column_name", "payload", "status"), row))

        # 审批是硬门槛，排在"支不支持这种变更"之前：没批的单，连讨论怎么执行都不该发生
        if args.action == "apply" and req["status"] != "approved":
            audit(conn, "gov.change_request", "denied", req["req_no"], None,
                  {"reason": "status is %s, not approved" % req["status"],
                   "change_kind": req["change_kind"]}, actor="simulator")
            conn.commit()
            print("[refused] %s 状态是 %s，没有审批不执行（已落 denied 审计）"
                  % (req["req_no"], req["status"]))
            return 1

        if req["change_kind"] not in HANDLERS:
            print("模拟器暂不支持这种变更：%s（不是所有变更都该自动化）" % req["change_kind"])
            return 2

        fn = HANDLERS[req["change_kind"]][0 if args.action == "apply" else 1]
        sql = fn(conn, req)
        if args.action == "apply":
            conn.execute("update gov.change_request set status='executed', executed_at=now() "
                         "where req_no=%s", (req["req_no"],))
        else:
            conn.execute("update gov.change_request set status='approved', executed_at=null "
                         "where req_no=%s", (req["req_no"],))
        audit(conn, "gov.change_request", "ddl", req["req_no"],
              {"status": "approved" if args.action == "apply" else "executed"},
              {"action": args.action, "sql": sql, "status": "executed" if args.action == "apply" else "approved"},
              actor="simulator")
        conn.commit()
        print("[ok] %s %s：%s" % (args.req, args.action, sql))
    return 0


if __name__ == "__main__":
    sys.exit(main())