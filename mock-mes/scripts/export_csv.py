#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按客户导出任务的样子导 CSV：字段集、列顺序、编码都能改。

三个可变量本身就是证据：
  --columns 决定字段集（客户导出的字段集和库里的列不总是一致）
  --encoding 决定编码（客户十个里八个给 GBK，判据要 UTF-8）
  日期按 ISO 渲染（库里是 date/timestamptz），客户 CSV 给的是 %Y/%m/%d
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get(
    "FDE_CLIENT_RO_DSN",
    "postgresql://ro_reader:roreader2026pw@localhost:5432/fde_client",
)

DEFAULTS = {
    "biz_defect_record": [
        "rec_id", "captured_at", "equip_no", "work_station",
        "defect_name", "conf_pct", "lot_no", "judge", "is_ok",
    ],
    "md_device": [
        "equip_no", "prod_line", "work_station", "cam_model",
        "lens_spec", "lamp_type", "install_dt", "maint_cycle",
    ],
}


def cell(v):
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ")
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="biz_defect_record")
    ap.add_argument("--columns", nargs="*", default=None)
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cols = args.columns or DEFAULTS.get(args.table)
    if not cols:
        print("没有默认字段集，请用 --columns 指定", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parent.parent / "out" / "csv" / f"{args.table}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    sql = "select %s from %s order by 1" % (", ".join(cols), args.table)
    if args.limit:
        sql += " limit %d" % args.limit

    n = 0
    with psycopg.connect(DSN, row_factory=dict_row) as conn, out.open("w", encoding=args.encoding, newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for row in conn.execute(sql):
            w.writerow([cell(row[c]) for c in cols])
            n += 1

    print("[ok] %d 行 -> %s（编码 %s，%d 列）" % (n, out, args.encoding, len(cols)))
    with out.open("r", encoding=args.encoding) as fh:
        print("     表头: %s" % next(fh).strip())
        print("     首行: %s" % next(fh).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())