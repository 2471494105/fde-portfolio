#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用只读账号取数，并自证越权会被拒。

这是三条取数路里最"强"的一条：走的是元数据，空列也看得见；代价是只能读，且看不到治理库。
退出码：0 = 读通且越权被拒，1 = 某一条不符合预期。
"""
from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get(
    "FDE_CLIENT_RO_DSN",
    "postgresql://ro_reader:roreader2026pw@localhost:5432/fde_client",
)

READ_SQL = "select rec_id, captured_at, equip_no, defect_name, conf_pct, lot_no, judge from biz_defect_record order by rec_id limit 3"
DENY_SQL = "select count(*) from gov.field_registry"


def main() -> int:
    ok = True
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        total = conn.execute("select count(*) as n from biz_defect_record").fetchone()["n"]
        print("[ok] biz_defect_record 共 %d 行" % total)
        for r in conn.execute(READ_SQL).fetchall():
            print("     %s | %s | %s | conf=%s | lot=%r"
                  % (r["rec_id"], r["captured_at"], r["equip_no"], r["conf_pct"], r["lot_no"]))

        try:
            conn.execute(DENY_SQL).fetchone()
            print("[FAIL] ro_reader 竟然读到了 gov.field_registry")
            ok = False
        except psycopg.errors.InsufficientPrivilege as exc:
            print("[ok] 越权被拒：%s" % str(exc).splitlines()[0])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())