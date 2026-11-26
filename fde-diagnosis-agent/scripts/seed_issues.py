#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 app/ds/issues.py 的类别登记同步进 `gov_issue_catalog`（页面读表）。

为什么不让页面读 Python：中控台是只读进程，读的是库；跨进程读另一个包里的
.py 会把「一个服务」变成「两个仓库的耦合」。所以 Python 是唯一源，脚本负责搬运，
`--check` 用来在验收里确认「表与源一致」——不一致就红，而不是等人肉比对。

用法
----
    python -X utf8 scripts/seed_issues.py            # 同步（幂等，可重复跑）
    python -X utf8 scripts/seed_issues.py --check     # 只比对，不一致返回 1
    python -X utf8 scripts/seed_issues.py --prune     # 顺带删掉源里已经没有的类别
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import issues  # noqa: E402

DSN = os.environ.get("FDE_CLIENT_DSN", "postgresql://postgres:postgres@localhost:5432/fde_client")
COLS = ("issue_id", "sort_no", "short", "name", "check_id", "owner", "first_step", "evidence", "note")


def connect(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def load(cur) -> dict[str, dict]:
    cur.execute("select * from gov_issue_catalog")
    return {r["issue_id"]: {k: r[k] for k in COLS} for r in cur.fetchall()}


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    prune = "--prune" in argv
    dsn = DSN
    for a in argv:
        if a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]

    src = {c["issue_id"]: {k: c.get(k) for k in COLS} for c in issues.CATALOG}
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "create table if not exists public.gov_issue_catalog ("
            " issue_id text primary key, sort_no integer not null, short text not null,"
            " name text not null, check_id text not null, owner text, first_step text not null,"
            " evidence text not null, note text, updated_at timestamptz not null default now())"
        )
        have = load(cur)
        if check_only:
            drift = []
            for iid in sorted(set(src) | set(have)):
                s, h = src.get(iid), have.get(iid)
                if s != h:
                    drift.append(iid)
            if drift:
                print(f"[FAIL] 表与源不一致：{', '.join(drift)}（跑一次 seed_issues.py 同步）")
                return 1
            print(f"[OK] {len(src)} 类与 gov_issue_catalog 一致")
            return 0

        for c in issues.CATALOG:
            cur.execute(
                "insert into gov_issue_catalog (issue_id, sort_no, short, name, check_id, owner,"
                " first_step, evidence, note) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (issue_id) do update set sort_no=excluded.sort_no, short=excluded.short,"
                " name=excluded.name, check_id=excluded.check_id, owner=excluded.owner,"
                " first_step=excluded.first_step, evidence=excluded.evidence, note=excluded.note,"
                " updated_at=now()",
                (c["issue_id"], c["sort_no"], c["short"], c["name"], c["check_id"],
                 c.get("owner"), c["first_step"], c["evidence"], c.get("note")),
            )
        if prune:
            gone = sorted(set(have) - set(src))
            for iid in gone:
                cur.execute("delete from gov_issue_catalog where issue_id = %s", (iid,))
            if gone:
                print(f"删除 {len(gone)} 类（源里已经没有）：{', '.join(gone)}")
        cur.execute("select count(*) as n from gov_issue_catalog")
        n = cur.fetchone()["n"]
    print(f"[OK] 已同步 {len(issues.CATALOG)} 类；表内 {n} 行")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
