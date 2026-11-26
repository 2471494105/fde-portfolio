"""判据 I / J：工位契约与缺口认领。

判据 I —— 明细表交不出工位就是违约
------------------------------------
工单 60 张写「1号工位」，缺陷 302 行的 `work_station` 一列混着「产线A-01」与
「4号线」，主数据是「产线A-01」。以前的做法是拿 `md_device.work_station` 反推：
设备台账知道哪台设备在哪个工位。实测只对 20/60，40 张矛盾——WO-0002 写「2号工位」，
它那台 CX-102 却在产线A-01（第 1 工位）。用设备反推 = 拿另一条同样可疑的线去修
这条线，再把结果写成事实。

所以 I 只做两件事：查 `md_station` 字典、把交不出工位的行连同单号列出来。
`station_id` 优先；没有就要求 `prod_line` + 工位文本，靠 (line_no, seq) 唯一匹配。
匹配不到、匹配多条、文本里没有工位号——都判红，**不猜**。

判据 J —— 缺口要有主、有期
---------------------------
红出来的东西不能只活在终端里。`data_contract.json` 的 `field_gaps` 把「缺什么、
谁补、什么时候补」登记下来：
  未写责任方 / 未写到期日 / 已过期仍有违约  → 红
  已认领、未过期、仍有违约                → 黄（提示，不判死）
  违约归零                                → 自动闭合（不需要人签字说修好了）

判据 I 自己的完整性也在这里查：`md_station` 没有 station_id 列、id 不合 pattern、
id 重复、16 行变了——契约坏了，下游再对也没意义。

用法：
    python scripts/check_station_contract.py
    python scripts/check_station_contract.py --today=2026-12-10     # 验「过期转红」
    python scripts/check_station_contract.py --json=eval/station_contract.json
    python scripts/check_station_contract.py --skip-db              # 只查登记表自洽
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import station_contract as sc  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"


def load_tables(registry: dict, dsn: str = CLIENT_DSN) -> tuple[dict[str, list[dict]], list[dict]]:
    """一次把主数据与各契约表取全。缺表就抛，让上面按「跳过」记，不当绿。"""
    import psycopg
    from psycopg.rows import dict_row

    key = registry["station_key"]
    tables: dict[str, list[dict]] = {}
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cols = ", ".join(
            [key["id_column"], key["business_code_column"], key["line_column"], key["seq_column"]]
        )
        cur.execute(f"SELECT {cols} FROM {key['master_table']} ORDER BY {key['id_column']}")
        master = [dict(r) for r in cur.fetchall()]

        for c in registry["contracts"]:
            cols = ", ".join([c["key_column"], c["field"], *c["alternatives"]])
            cur.execute(f"SELECT {cols} FROM {c['table']}")
            tables[c["id"]] = [dict(r) for r in cur.fetchall()]
    return tables, master


def check_registry(registry: dict) -> list[str]:
    """不连库也能查的：契约字段齐不齐、缺口指向的契约在不在、到期日格式对不对。"""
    problems: list[str] = []
    for c in registry.get("contracts") or []:
        for f in ("id", "table", "key_column", "field", "owner", "due", "parse_rule"):
            if not c.get(f):
                problems.append(f"契约 {c.get('id')!r} 缺字段 {f}：登记不完整就不能当契约用")
        try:
            date.fromisoformat(str(c.get("due")))
        except ValueError:
            problems.append(f"契约 {c.get('id')!r} 的 due 不是日期：{c.get('due')!r}")
    ids = {c.get("id") for c in registry.get("contracts") or []}
    for g in registry.get("field_gaps") or []:
        if g.get("contract") not in ids:
            problems.append(f"缺口 {g.get('id')!r} 指向未登记的契约 {g.get('contract')!r}")
        for f in ("id", "missing", "owner", "due"):
            if not g.get(f):
                problems.append(f"缺口 {g.get('id')!r} 缺字段 {f}")
        try:
            date.fromisoformat(str(g.get("due")))
        except ValueError:
            problems.append(f"缺口 {g.get('id')!r} 的 due 不是日期：{g.get('due')!r}")
    return problems


def main(argv: list) -> int:
    json_out = ""
    skip_db = False
    dsn = CLIENT_DSN
    today = date.today()
    for a in argv:
        if a.startswith("--json="):
            json_out = a.split("=", 1)[1]
        elif a == "--skip-db":
            skip_db = True
        elif a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--today="):
            today = date.fromisoformat(a.split("=", 1)[1])

    registry = sc.load()
    checks: dict[str, dict] = {}

    reg_problems = check_registry(registry)
    checks["I0_registry_selfcheck"] = {
        "status": "fail" if reg_problems else "ok",
        "detail": reg_problems,
        "note": [] if not reg_problems else ["登记表本身不成立，后面的结论不作数"],
    }

    if skip_db:
        checks["I_station_contract"] = {"status": "skipped", "detail": ["--skip-db"]}
        checks["J_gap_claimed"] = {"status": "skipped", "detail": ["--skip-db"]}
        summary = {"checks": checks, "status": "fail" if reg_problems else "skipped", "today": today.isoformat()}
    else:
        tables, master = load_tables(registry, dsn)
        report = sc.audit(registry, tables, master, today=today)
        checks["I_station_contract"] = {
            "status": "fail" if any(c["violations"] for c in report["contracts"]) or sc.check_station_key(registry, master) else "ok",
            "detail": sc.check_station_key(registry, master)
            + [
                f"{c['id']}: 违约 {c['violations']}/{c['total']}，样例 {', '.join(map(str, c['samples']))}"
                for c in report["contracts"]
                if c["violations"]
            ],
            "note": [f"{c['id']}: {c['total']} 行全部按契约交得出工位" for c in report["contracts"] if not c["violations"]],
        }
        gap_states = {g["state"] for g in report["gaps"]}
        checks["J_gap_claimed"] = {
            "status": "fail" if "red" in gap_states else ("warn" if "warn" in gap_states else "ok"),
            "detail": [f"{g['id']} [{g['state']}] {g['note']}" for g in report["gaps"]],
            "note": [],
        }
        summary = {
            "today": today.isoformat(),
            "checks": checks,
            "contracts": report["contracts"],
            "gaps": report["gaps"],
            "station_rows": report["station_rows"],
            "status": "fail" if report["problems"] or reg_problems else ("warn" if "warn" in gap_states else "ok"),
        }

    print(f"今天 {today.isoformat()}")
    for k, v in checks.items():
        print(f"[{v['status']:7s}] {k}")
        for d in v.get("detail") or []:
            print(f"          - {d}")
        for n in v.get("note") or []:
            print(f"          · {n}")

    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json → {out}")

    return 0 if summary["status"] in ("ok", "warn") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("检查脚本自身崩了（这一轮结果不可当结论）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
