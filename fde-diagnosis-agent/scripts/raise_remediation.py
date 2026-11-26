"""数据整改单：把判据红出来的东西，变成一张能被追的纸。

判据红只是一次输出，没人认领的红会被下一次运行覆盖掉。所以：

  1. `--raise`：判据 I 违约 > 0 的契约 → 生成整改单（表 / 字段 / 行数 / 样例单号 /
     依据契约 / 责任方 / 到期日）。同一契约在同一张表上只允许一张未关闭的单子，
     重复跑是更新数字，不是再开一张纸。
  2. `--close`：复跑判据，**违规归零才允许关闭**，并把「复跑时间 + 违规 0」写成
     关闭证据。还剩 N 行时拒绝关闭——纸面不能关闭数据问题。
  3. `--list`：当前单子与逾期天数。

`--orders-table` 可换表（验证脚本在 probe schema 里跑完整闭环，不污染真单子）。

用法：
    python scripts/raise_remediation.py --raise
    python scripts/raise_remediation.py --list
    python scripts/raise_remediation.py --close
"""

from __future__ import annotations

import sys
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.ds import station_contract as sc  # noqa: E402
import check_station_contract as csc  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
ORDERS_TABLE = "public.gov_remediation_order"


def _connect(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row)


def audit_now(registry: dict, dsn: str, today: date) -> dict:
    tables, master = csc.load_tables(registry, dsn)
    return sc.audit(registry, tables, master, today=today)


def next_rm_no(cur, today: date) -> str:
    cur.execute(
        "SELECT rm_no FROM %s WHERE rm_no LIKE %%s ORDER BY rm_no DESC LIMIT 1" % ORDERS_TABLE,
        (f"RM-{today:%Y%m%d}-%",),
    )
    row = cur.fetchone()
    n = int(row["rm_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"RM-{today:%Y%m%d}-{n:02d}"


def raise_orders(dsn: str, today: date, registry: dict | None = None) -> list[dict]:
    registry = registry or sc.load()
    report = audit_now(registry, dsn, today)
    events = []
    with _connect(dsn) as conn, conn.cursor() as cur:
        for c in report["contracts"]:
            if not c["violations"]:
                continue
            cur.execute(
                "SELECT rm_no, violation_count FROM %s WHERE contract_id = %%s AND table_name = %%s AND state = 'open'"
                % ORDERS_TABLE,
                (c["id"], c["table"]),
            )
            existing = cur.fetchone()
            samples = ", ".join(str(s) for s in c["samples"])
            if existing:
                cur.execute(
                    "UPDATE %s SET violation_count = %%s, samples = %%s, updated_at = now() WHERE rm_no = %%s"
                    % ORDERS_TABLE,
                    (c["violations"], samples, existing["rm_no"]),
                )
                events.append({"action": "updated", "rm_no": existing["rm_no"], "contract": c["id"], "violations": c["violations"]})
            else:
                rm_no = next_rm_no(cur, today)
                cur.execute(
                    "INSERT INTO %s (rm_no, contract_id, table_name, field_name, violation_count, samples, owner, due_on)"
                    " VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s)" % ORDERS_TABLE,
                    (rm_no, c["id"], c["table"], c["field"], c["violations"], samples, c["owner"], c["due"]),
                )
                events.append({"action": "created", "rm_no": rm_no, "contract": c["id"], "violations": c["violations"]})
    return events


def close_orders(dsn: str, today: date, registry: dict | None = None) -> list[dict]:
    registry = registry or sc.load()
    report = audit_now(registry, dsn, today)
    by_id = {c["id"]: c for c in report["contracts"]}
    events = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT rm_no, contract_id, table_name, violation_count FROM %s WHERE state = 'open' ORDER BY rm_no" % ORDERS_TABLE)
        for row in cur.fetchall():
            c = by_id.get(row["contract_id"])
            left = c["violations"] if c else None
            if left is None:
                events.append({"action": "refused", "rm_no": row["rm_no"], "why": "契约已不在登记表里，无法复跑"})
                continue
            if left > 0:
                cur.execute(
                    "UPDATE %s SET violation_count = %%s, updated_at = now(), samples = %%s WHERE rm_no = %%s" % ORDERS_TABLE,
                    (left, ", ".join(str(s) for s in c["samples"]), row["rm_no"]),
                )
                events.append({"action": "refused", "rm_no": row["rm_no"], "why": f"复跑仍有 {left} 行违约，不许关闭"})
                continue
            cur.execute(
                "UPDATE %s SET state = 'closed', closed_at = now(), violation_count = 0,"
                " close_evidence = %%s WHERE rm_no = %%s" % ORDERS_TABLE,
                (f"{stamp} 复跑 {c['total']} 行，违约 0（判据 I），据此关闭", row["rm_no"]),
            )
            events.append({"action": "closed", "rm_no": row["rm_no"], "total": c["total"]})
    return events


def list_orders(dsn: str, today: date) -> list[dict]:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rm_no, contract_id, table_name, field_name, violation_count, owner, due_on, state,"
            " closed_at, close_evidence FROM %s ORDER BY state, rm_no" % ORDERS_TABLE
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        due = r.get("due_on")
        r["overdue_days"] = (today - due).days if due and r["state"] == "open" else 0
    return rows


def main(argv: list) -> int:
    global ORDERS_TABLE
    dsn = CLIENT_DSN
    today = date.today()
    mode = "list"
    for a in argv:
        if a == "--raise":
            mode = "raise"
        elif a == "--close":
            mode = "close"
        elif a == "--list":
            mode = "list"
        elif a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--orders-table="):
            ORDERS_TABLE = a.split("=", 1)[1]
        elif a.startswith("--today="):
            today = date.fromisoformat(a.split("=", 1)[1])

    if mode == "raise":
        for e in raise_orders(dsn, today):
            print(f"[{e['action']}] {e['rm_no']} {e['contract']}：违约 {e['violations']} 行")
    elif mode == "close":
        for e in close_orders(dsn, today):
            print(f"[{e['action']}] {e['rm_no']}：{e.get('why') or '已关闭（违约 0）'}")
    rows = list_orders(dsn, today)
    print(f"\n{ORDERS_TABLE} @ {dsn.rsplit('/', 1)[-1]}  今天 {today.isoformat()}")
    if not rows:
        print("  （没有整改单）")
        return 0
    for r in rows:
        tail = r.get("close_evidence") or f"到期 {r['due_on']}" + (f"，逾期 {r['overdue_days']} 天" if r["overdue_days"] > 0 else "")
        print(f"  {r['rm_no']} [{r['state']}] {r['contract_id']} {r['table_name']}.{r['field_name']}"
              f" 违约 {r['violation_count']} / 责任 {r['owner']} / {tail}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("整改单脚本自身崩了：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
