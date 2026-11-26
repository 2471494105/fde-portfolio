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
  D 折法完整性（K-5）        —— 登记了词表就要说清每个词折成我方哪一态；
                                我方基准词在这家客户身上没有落点的，必须在 fold_gaps_states 认领
  E 不适用声明（T14）        —— `unsupported_checks` 的 id 必须在 `checks` 登记表里，
                                而且真跑一遍入口：声明不适用的必须 `ran=False`，
                                没声明的必须 `ran=True`（「被静默跳过」也要报）

判据 B 需要连库；`--skip-db` 只跑 A/C/D/E。连不上库时状态是 `skipped`，**不是绿**。
B 的观测面默认是 `fde_rag.work_orders`（本家库）；换客户时用 `--dsn=` 与 `--table=`
把它指到客户库，否则「查的是自己家的词、说的是客户家的事」。

用法：
    python scripts/check_status_policy.py
    python scripts/check_status_policy.py --json=eval/status_policy.json
    python scripts/check_status_policy.py --skip-db
    python scripts/check_status_policy.py --dsn=postgresql://.../fde_client --table=wo_b
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


def check_db_states(policy: dict, dsn: str = DSN, table: str = "work_orders") -> dict:
    """B：库里实际出现的状态，是否都在口径表的已知集合里。

    观测面可换：默认查本家库 `fde_rag.work_orders`；换客户时用 `--dsn` / `--table`
    指到客户库，否则就是拿自己家的词去说客户家的事。
    """
    try:
        import psycopg
    except Exception as e:  # 依赖缺失也是「没验」，不是绿
        return {"status": "skipped", "detail": [f"psycopg 不可用：{e}"]}
    try:
        sql = f"SELECT DISTINCT state FROM {table} ORDER BY state"
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            try:
                cur.execute(sql)
            except Exception:
                conn.rollback()
                cur.execute(f"SELECT DISTINCT status FROM {table} ORDER BY status")
            actual = tuple(r[0] for r in cur.fetchall() if r[0] is not None)
        client = sp.client(policy.get("default_client"), policy)
        unknown = sp.unknown_states(actual, client)
        return {
            "status": "fail" if unknown else "ok",
            "source": f"{table} @ {dsn.rsplit('/', 1)[-1]}",
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


def check_fold(policy: dict) -> dict:
    """D：折法完整性——登记了词表，就要说清每个词折成我方哪一态。

    A/B/C 都只问「这家客户自己内部自不自洽」，问不到「和我们的基准对不对得上」。
    换客户的真正风险在后一半：词表自洽、折法缺失，保护集就会静默失真。
    """
    details = []
    checked = []
    for name, c in policy["clients"].items():
        if not c.get("registered"):
            continue
        checked.append(name)
        details += [f"{name}：{p}" for p in sp.fold_problems(name, policy)]
    return {
        "status": "fail" if details else "ok",
        "checked": checked,
        "detail": details,
    }


def check_unsupported(policy: dict) -> dict:
    """E：`unsupported_checks` 不能只是一句话，要能被机器对上。

    两层：
      静 —— id 是否都在 `checks` 登记表里、有没有写理由、是不是所有客户都把它关掉了；
      动 —— 真调一次 `run_check`：声明不适用的必须 `ran=False` 且 `not_applicable`，
            没声明的必须 `ran=True`。**「声明了但还在跑」和「没声明却被跳过」都报。**
    动的这一层是这条判据的全部意义：静态对账只能证明「字对上了」，证明不了「没跑」。
    """
    from app.ds import checks as ck

    detail = list(sp.check_problems(policy))
    checked = []
    for name, c in sorted(policy["clients"].items()):
        if not c.get("registered"):
            continue
        declared = c.get("unsupported_checks") or {}
        if isinstance(declared, list):
            continue  # 已在静态那层报过「旧写法」
        for cid in sorted(sp.checks_registry(policy)):
            checked.append(f"{name}/{cid}")
            try:
                r = ck.run_check(cid, rows=[], name=name, policy=policy)
            except Exception as e:
                detail.append(f"{name}/{cid}：入口抛 {type(e).__name__}——{e}")
                continue
            if cid in declared:
                if r.get("ran") or r.get("status") != "not_applicable":
                    detail.append(
                        f"{name}/{cid}：声明不适用，但入口仍然跑了（ran={r.get('ran')}、"
                        f"status={r.get('status')}）——声明没落到代码里，每天会造一批假红")
            elif not r.get("ran"):
                detail.append(
                    f"{name}/{cid}：没声明不适用，入口却没跑（status={r.get('status')}）"
                    "——判据被静默跳过，比跑错更难发现")
    return {
        "status": "fail" if detail else "ok",
        "checked": checked,
        "detail": detail,
    }


def check_entry_bypass(policy: dict) -> dict:
    """F：实现只许从 `run_check` 进去（AST 扫全仓库）。

    E 判据验的是「入口在不适用的客户身上没跑」；有人绕过入口直调实现，E 就瞎了。
    所以「只许走入口」不能停在约定上，得能扫出来。
    """
    from app.ds import checks as ck

    detail = ck.bypass_problems(ROOT)
    return {"status": "fail" if detail else "ok", "detail": detail}


def main(argv: list) -> int:
    json_out = ""
    skip_db = False
    dsn = DSN
    table = "work_orders"
    for a in argv:
        if a.startswith("--json="):
            json_out = a.split("=", 1)[1]
        elif a == "--skip-db":
            skip_db = True
        elif a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--table="):
            table = a.split("=", 1)[1]

    policy = sp.load()
    checks = {
        "A_schema_index_drift": check_schema_drift(policy),
        "B_unregistered_db_states": {"status": "skipped", "detail": ["--skip-db"]}
        if skip_db else check_db_states(policy, dsn, table),
        "C_unregistered_client_refuses": check_unregistered_refuses(policy),
        "D_fold_completeness": check_fold(policy),
        "E_unsupported_checks": check_unsupported(policy),
        "F_check_entry_bypass": check_entry_bypass(policy),
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
