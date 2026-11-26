#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 gov.field_registry 导出成与 client_a_devices.json 同构的 JSON。

为什么要同构：判据（source_schema.load / diff / breaking）只认那一种形状。
登记册的存法是我们的自由，交付给判据的形状不是。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

AGENT_REGISTRY = Path(__file__).resolve().parents[2] / "fde-diagnosis-agent" / "data" / "mapping" / "source_schema"

import psycopg
from psycopg.rows import dict_row

CLIENT = "client_mes"
LABELS = {
    "md_device": "设备台账",
    "biz_defect_record": "缺陷记录",
    "biz_work_order": "维修工单",
}
DEFAULT_DSN = os.environ.get(
    "FDE_CLIENT_DSN",
    "postgresql://qms_app:qmsapp2026pw@localhost:5432/fde_client",
)

SQL = """
select column_name, ordinal, data_type, is_nullable, is_key,
       unit, domain_kind, domain_spec, note
from gov.field_registry
where client = %s and table_name = %s and is_active
order by ordinal
"""


def domain_of(kind: str, spec: dict, unit: str | None, note: str | None) -> dict:
    d: dict = {}
    if kind == "range":
        d["range"] = [spec.get("min"), spec.get("max")]
    elif kind == "format":
        if spec.get("format") is not None:
            d["format"] = spec["format"]
        if spec.get("regex") is not None:
            d["regex"] = spec["regex"]
    elif kind == "enum":
        d["enum"] = spec.get("values")
    else:
        d[kind] = True
    if spec.get("severity"):
        d["severity"] = spec["severity"]
    if unit:
        d["unit"] = unit
    if note:
        d["note"] = note
    return d


def build(rows: list[dict], table: str) -> dict:
    keys = [r["column_name"] for r in rows if r["is_key"]]
    domains: dict[str, dict] = {}
    for r in rows:
        if not r["domain_kind"]:
            continue
        domains[r["column_name"]] = domain_of(
            r["domain_kind"], dict(r["domain_spec"] or {}), r["unit"], r["note"]
        )
    return {
        "client": CLIENT,
        "table": table,
        "label": LABELS.get(table, table),
        "observed_via": "db_metadata",
        "observed_at": date.today().isoformat(),
        "source_path": "postgresql://localhost:5432/fde_client (public.%s)" % table,
        "key_columns": keys,
        "renames": {},
        # 登记要给判据的不只是列名：类型和必填是 information_schema 本来就给得出的，
        # 只写名字等于把这两维主动退化成"未观测"——S1 会一直报"类型未观测 8 列"，
        # 而真正改成 text->integer 的那一天，它也只会继续报"未观测"。
        "columns": [
            {"name": r["column_name"],
             "type": r["data_type"],
             "required": (not bool(r["is_nullable"])) if r["is_nullable"] is not None else None}
            for r in rows
        ],
        "limits": (
            "库元数据给出类型与必填（is_nullable），列集合可信、空列也可见；"
            "值域取 gov.field_registry 的 domain 声明，未声明的不参与判定。"
        ),
        "domains": domains,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--tables", nargs="*", default=list(LABELS))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "out" / "registry"))
    ap.add_argument("--sync-to", default=str(AGENT_REGISTRY),
                    help="顺手同步给判据读的那份副本；传空串则不复制")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        for table in args.tables:
            rows = conn.execute(SQL, (CLIENT, table)).fetchall()
            if not rows:
                print(f"[skip] {CLIENT}.{table} 登记册里没有行", file=sys.stderr)
                continue
            payload = build(rows, table)
            path = out_dir / f"{CLIENT}_{table}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written.append((table, len(payload["columns"]), path))
    for table, ncol, path in written:
        print(f"[ok] {table}: {ncol} 列 -> {path}")

    # 判据读的是 agent 目录下那份副本。只写自己目录、不同步，副本就会一直停在旧版本，
    # 于是"登记册改了、判据还红"—— 这种不一致不是判据的错，是导出这一步漏了。
    if args.sync_to:
        dest = Path(args.sync_to)
        if dest.exists():
            for _, _, path in written:
                shutil.copy2(path, dest / path.name)
            print(f"[sync] {len(written)} 份 -> {dest}")
        else:
            print(f"[warn] 目标目录不存在，跳过同步：{dest}", file=sys.stderr)
    return 0 if written else 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main())