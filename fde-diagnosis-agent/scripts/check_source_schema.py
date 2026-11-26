"""源端漂移判据：登记的列还在不在，新加的列是不是我们该收的。

为什么需要它：我方两条漂移判据（CONTENT_COLS / FACT_KEYS）读的都是我方表，
源端加一列我们永远看不见。这里把观测面挪到源端本身。

四条判据：
  S1 源端漂移   登记的列 vs 现在的列：加、删、改类型、改必填，逐项列
  S2 未消费列   源有、但我们没消费 —— **黄不是红**，这是「有意识忽略」清单
  S3 破坏性     主键/游标列消失、新增必填列 —— 拒跑
  S4 未声明改名 一加一减且不在 renames 里 —— 拒，不猜「看着像」

用法：
    python scripts/check_source_schema.py                      # 默认 client_a 的全部已登记表
    python scripts/check_source_schema.py --table=devices
    python scripts/check_source_schema.py --source=<另一份 CSV>  # 反例探针，不动原文件
    python scripts/check_source_schema.py --json=eval/source_schema.json
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import source_schema as ss  # noqa: E402

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"


def mapping_consumed(client: str, table: str) -> list:
    """映射里 `from` 指向的源列 = 我们真正读的列。没配映射就返回空（S2 会全亮，属正常）。"""
    p = ROOT / "data" / "mapping" / f"{client}.json"
    if not p.exists():
        return []
    spec = json.loads(p.read_text(encoding="utf-8"))
    fields = ((spec.get("tables") or {}).get(table) or {}).get("fields") or {}
    return [cfg.get("from", target) for target, cfg in fields.items()]


def resolve_target(reg: dict, db_table: str | None) -> tuple[str, str]:
    """登记里的 source_path 可能长这样：`postgresql://.../fde_client (public.md_device)`。

    括号里才是 schema 和表名；前面的连接串只说明"这份登记是连哪个库读出来的"。
    """
    raw = db_table or reg["source_path"]
    m = re.search(r"\(([^)]+)\)", raw)
    if m:
        raw = m.group(1).strip()
    if "." in raw:
        schema, table = raw.split(".", 1)
        return schema, table
    return "public", raw


def observe(reg: dict, source: str | None, db_table: str | None, dsn: str | None = None) -> tuple[dict, str]:
    via = "db" if db_table else (reg.get("observed_via") or "csv_header")
    if via == "csv_header":
        src = Path(source) if source else ROOT / reg["source_path"]
        return ss.observe_csv_header(src), f"csv_header({src.name})"
    if via in ("db", "db_metadata"):
        # db_metadata 与 db 是同一条路：都问 information_schema。分开命名只是把信号来源记清楚。
        import psycopg
        schema, target = resolve_target(reg, db_table)
        with psycopg.connect(dsn or DEFAULT_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, target),
            )
            rows = cur.fetchall()
        if not rows:
            raise RuntimeError(f"库里没有表 {schema}.{target}（先建表再观测）")
        return ss.parse_information_schema(rows), f"information_schema({schema}.{target})"
    raise RuntimeError(f"不认识的 observed_via：{via}")


def check_one(client: str, table: str, source: str | None, db_table: str | None,
              dsn: str | None = None) -> dict:
    reg = ss.load(client, table)
    observed, via_label = observe(reg, source, db_table, dsn)
    diffres = ss.diff(reg, observed)
    diffres["observed"] = observed
    breaks = ss.breaking(diffres, reg)
    not_used = ss.unconsumed(observed, mapping_consumed(client, table))
    red = (not diffres["clean"]) or bool(diffres["rename_suspect"])
    return {
        "client": client,
        "table": table,
        "observed_via": via_label,
        "registered_at": reg.get("observed_at"),
        "s1_drift": {k: diffres[k] for k in
                     ("added", "removed", "type_changed", "type_unchecked",
                      "required_changed", "renamed", "rename_suspect")},
        "s2_unconsumed": not_used,
        "s3_breaking": breaks,
        "status": "fail" if (breaks or red) else "ok",
        "exit": 3 if breaks else (1 if red else 0),
    }


def main(argv: list) -> int:
    client = "client_a"
    only_table = ""
    source = ""
    db_table = ""
    json_out = ""
    dsn = ""
    for a in argv:
        if a.startswith("--client="):
            client = a.split("=", 1)[1]
        elif a.startswith("--table="):
            only_table = a.split("=", 1)[1]
        elif a.startswith("--source="):
            source = a.split("=", 1)[1]
        elif a.startswith("--db-table="):
            db_table = a.split("=", 1)[1]
        elif a.startswith("--json="):
            json_out = a.split("=", 1)[1]
        elif a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]

    reg_dir = ss.REGISTRY_DIR
    # 文件名是 <client>_<table>.json，而 client 名里本身带下划线（client_a），
    # 所以只能按前缀长度切，不能用 split("_")。
    tables = sorted(
        p.stem[len(client) + 1:] for p in reg_dir.glob(f"{client}_*.json")
    ) if reg_dir.exists() else []
    if only_table and only_table != "all":
        tables = [only_table] if only_table in tables else [only_table]

    results, worst = [], 0
    for t in tables:
        r = check_one(client, t, source or None, db_table or None, dsn or None)
        results.append(r)
        worst = max(worst, r["exit"])
        d = r["s1_drift"]
        head = "ok" if r["status"] == "ok" else "FAIL"
        print(f"[{head}] {client}/{t}  via={r['observed_via']}  登记于 {r['registered_at']}")
        if d["added"]:
            print(f"        S1 新增列 {d['added']} —— 我们没配映射的话，它进不来也不报错")
        if d["removed"]:
            print(f"        S1 消失列 {d['removed']} —— 若它是必填/主键，下面 S3 会拒跑")
        for tc in d["type_changed"]:
            print(f"        S1 类型变 {tc['column']}: {tc['was']} -> {tc['now']}")
        for rc in d["required_changed"]:
            print(f"        S1 必填变 {rc['column']}: {rc['was']} -> {rc['now']}")
        if d["type_unchecked"]:
            print(f"        S1 类型未观测 {len(d['type_unchecked'])} 列（表头给不出类型，不参与判定）")
        if r["s2_unconsumed"]:
            print(f"        S2 未消费 {r['s2_unconsumed']}（黄：给个「有意识忽略」的理由即可）")
        for b in r["s3_breaking"]:
            print(f"        S3 拒跑 {b}")
        if d["rename_suspect"] and not d["renamed"]:
            print(f"        S4 疑似改名（一加一减）：{d['rename_suspect']} —— 要改就往 renames 里声明")

    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"client": client, "tables": results}, ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        print(f"json -> {out}")

    return worst


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("判据脚本自身崩了（这一轮结果不可当结论）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)