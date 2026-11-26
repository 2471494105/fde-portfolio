"""口径漂移检查：三处字面量现在只剩一处，而且有人看着。

2026-11-26 之前，「闭环」这三个词在同一套系统里写了**四处**：
  - `data/schema.sql` 的 `work_orders_open_key` 部分唯一索引谓词
  - `mock_qms/reset.py` 的保护集反查
  - `scripts/check_ds_delete.py` 的 `OPEN_STATUS_EXCLUDE`
  - `scripts/check_data_contract.py` 的 A3 断言

四处各写一份，漏改任一处都不报错，只让某一处的行为悄悄变。现在前三处改为读
`data/mapping/status_policy.json`；剩下索引谓词**不能读 JSON**（可索引表达式必须
不可变，读表的函数不行），所以它留字面量，由本脚本盯：

  A 索引谓词 vs 口径表       —— 不一致就被判红（否则索引还在按旧词判闭环）
  B 库里出现未登记的状态     —— 既不算闭环也不算未闭环，代码会机械地把它归进某一类
  C 未登记客户必须拒跑       —— 反例自证：它得抛 `PolicyNotRegistered`，不抛就红

判据 B 需要连库；`--skip-db` 只跑 A 与 C。连不上库时状态是 `skipped`，**不是绿**。

用法：
    python scripts/check_status_policy.py
    python scripts/check_status_policy.py --json=eval/status_policy.json
    python scripts/check_status_policy.py --skip-db
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import status_policy as sp  # noqa: E402

DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"
SCHEMA = ROOT / "data" / "schema.sql"


def check_schema_drift(policy: dict) -> dict:
    """A：索引谓词与口径表是否一致。"""
    text = SCHEMA.read_text(encoding="utf-8")
    schema_states = sp.schema_predicate_states(text)
    client_name = policy.get("default_client")
    registered = [
        (n, c) for n, c in policy["clients"].items() if c.get("registered")
    ]
    if not registered:
        return {"status": "fail", "detail": ["口径表里没有任何已登记客户"]}
    name, c = registered[0]
    problems = sp.drift(schema_states, tuple(c.get("closed_states") or []))
    return {
        "status": "fail" if problems else "ok",
        "schema_states": list(schema_states),
        "policy_client": name,
        "policy_states": list(c.get("closed_states") or []),
        "detail": problems,
    }


def check_db_states(policy: dict) -> dict:
    """B：库里实际出现的状态，是否都在口径表的已知集合里。"""
    try:
        import psycopg
    except Exception as e:  # 依赖缺失也是「没验」，不是绿
        return {"status": "skipped", "detail": [f"psycopg 不可用：{e}"]}
    try:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status FROM work_orders ORDER BY status")
            actual = tuple(r[0] for r in cur.fetchall() if r[0] is not None)
        client = sp.client(policy.get("default_client"), policy)
        unknown = sp.unknown_states(actual, client)
        return {
            "status": "fail" if unknown else "ok",
            "actual_states": list(actual),
            "unknown_states": unknown,
            "detail": [f"库里出现未登记状态 {unknown}，它既不算闭环也不算未闭环"]
            if unknown else [],
        }
    except Exception as e:
        return {"status": "skipped", "detail": [f"连不上库（{type(e).__name__}）：{e}"]}


def check_unregistered_refuses(policy: dict) -> dict:
    """C：未登记客户必须拒跑，不许返回空词表。"""
    details = []
    seen = []
    for name, c in policy["clients"].items():
        if c.get("registered"):
            continue
        seen.append(name)
        try:
            sp.open_predicate("status", name, policy)
            details.append(f"{name} 未登记却返回了谓词——这正是要拒的那种猜法")
        except sp.PolicyNotRegistered:
            continue
        except Exception as e:
            details.append(f"{name} 抛的不是 PolicyNotRegistered 而是 {type(e).__name__}")
    return {
        "status": "fail" if details else "ok",
        "checked": seen,
        "detail": details,
    }


def main(argv: list) -> int:
    json_out = ""
    skip_db = False
    for a in argv:
        if a.startswith("--json="):
            json_out = a.split("=", 1)[1]
        elif a == "--skip-db":
            skip_db = True

    policy = sp.load()
    checks = {
        "A_schema_index_drift": check_schema_drift(policy),
        "B_unregistered_db_states": {"status": "skipped", "detail": ["--skip-db"]}
        if skip_db else check_db_states(policy),
        "C_unregistered_client_refuses": check_unregistered_refuses(policy),
    }
    ok = all(c["status"] in ("ok", "skipped") for c in checks.values()) and any(
        c["status"] == "ok" for c in checks.values()
    )
    summary = {
        "policy": sp.policy_summary(policy=policy),
        "checks": checks,
        "status": "ok" if ok else "fail",
    }

    for k, v in checks.items():
        print(f"[{v['status']:7s}] {k}")
        for d in v.get("detail") or []:
            print(f"          - {d}")

    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json → {out}")

    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("检查脚本自身崩了（这一轮结果不可当结论）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
