"""判据 K：缺陷表的行级字段面（越界 / 未来时间 / 空批次）。

为什么现在补
------------
缺陷页从 T10 起就把 5 行脏数据染了色——红=置信度越界、橙=未来时间、灰=批次空。
但 Python 侧**一条判据都没有**：颜色是页面自己判的，判据沉默了。这就是 DOC-60
「同一条规则写了四遍」的同一种病换了个页面：页面红了，治理结论里却什么都没发生，
一旦有人问「这 4 条判据在哪」，答案是「没有」。

顺带把「空批次」拆成两件事：NULL 与空串。R2 的「空串≠缺失≠0」在这里落地——
两者都红，但修复动作不同：NULL 查映射与源端（源端没给这一列），空串查录入。

判定时刻可注入
--------------
未来时间这条要拿「现在」比。页面取浏览器时钟、判据取库里时钟，两个钟各自走，
边界行就会随机对不上。所以两侧都接受 `--now`（页面是 opts.now），对账时喂同一个
字面时刻；默认用库里的 `now()`，因为「库里那一刻」才是数据该被判断的口径。

用法：
    python scripts/check_defect_fields.py
    python scripts/check_defect_fields.py --now=2026-11-26 23:00:00   # 指定判定时刻
    python scripts/check_defect_fields.py --json=eval/defect_fields.json
    python scripts/check_defect_fields.py --no-record                  # 只判不记账
    python scripts/check_defect_fields.py --dsn=... --table=probe.df    # 换观测面（反例用）
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import field_checks as fc  # noqa: E402
from app.ds import runlog  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
SPEC_PATH = ROOT / "data" / "mapping" / "field_rules.json"


def load_spec(path: Path = SPEC_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_rows(spec: dict, dsn: str, table: str) -> tuple[list[dict], str]:
    """取这一张表里规则读到的所有列 + 主键。`now` 用库里的，避免本机时钟漂。"""
    import psycopg
    from psycopg.rows import dict_row

    cols: list[str] = [spec["key"]]
    for r in (spec.get("rules") or {}).values():
        for c in r.get("reads") or []:
            if c not in cols:
                cols.append(c)
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM {table}")
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT now()")
        db_now = cur.fetchone()["now"]
    return rows, db_now


def main(argv: list[str]) -> int:
    dsn, table, now, json_out = CLIENT_DSN, None, None, None
    record_run = True
    for a in argv:
        if a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--table="):
            table = a.split("=", 1)[1]
        elif a.startswith("--now="):
            now = a.split("=", 1)[1]
        elif a.startswith("--json="):
            json_out = a.split("=", 1)[1]
        elif a == "--no-record":
            record_run = False
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    t0 = time.perf_counter()
    spec = load_spec()
    table = table or spec["table"]

    try:
        rows, db_now = load_rows(spec, dsn, table)
    except Exception as e:
        print(f"[error] 取数失败（{table} @ {dsn}）：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    now = now or db_now.strftime("%Y-%m-%d %H:%M:%S")
    now_dt = datetime.fromisoformat(now.replace(" ", "T", 1))
    print(f"观测面 {table} @ {dsn.rsplit('/', 1)[-1]}，{len(rows)} 行")
    print(f"判定时刻 {now_dt.isoformat()}（{'注入' if argv and any(a.startswith('--now=') for a in argv) else '库内 now()'}）")

    results: dict[str, dict] = {}
    problems_all: list[str] = []
    ids_all: set[str] = set()
    for rule_id in (spec.get("rules") or {}):
        try:
            res = fc.run(rule_id, rows, spec, now)
        except Exception as e:
            print(f"[error] {rule_id} 跑挂了（这一条不算通过）：{type(e).__name__}: {e}", file=sys.stderr)
            results[rule_id] = {"ran": False, "status": "error", "checked": 0, "problems": [str(e)], "ids": []}
            problems_all.append(f"{rule_id}: {e}")
            continue
        results[rule_id] = res
        problems_all.extend(res["problems"])
        ids_all.update(res["ids"])
        tag = "fail" if res["status"] == "fail" else "ok"
        extra = ""
        if res.get("null_ids") or res.get("empty_ids"):
            extra = f"（NULL {len(res['null_ids'])} · 空串 {len(res['empty_ids'])}）"
        print(f"[{tag:7s}] {rule_id}：{len(res['ids'])}/{res['checked']} 行{extra}")
        for p in res["problems"]:
            print(f"          - {p}")

    red_rows = len(ids_all)
    status = "fail" if problems_all else "ok"
    summary = {
        "table": table,
        "rows": len(rows),
        "now": now_dt.isoformat(),
        "status": status,
        "red_rows": red_rows,
        "rules": results,
        "red_ids": sorted(ids_all, key=lambda s: (len(s), s)),
    }
    print(f"\n红 {red_rows} 行（一条行可同时踩多条规则，按编号去重）")

    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json → {out}")

    if record_run:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        details = {
            "red_metric": "violating_rows",
            "rows": {k: len(v.get("ids") or []) for k, v in results.items()},
            "now_override": now if any(a.startswith("--now=") for a in argv) else None,
            "table": table,
        }
        try:
            run_id = runlog.record(
                "defect_fields", status, red_rows, duration_ms,
                dataset=f"fde_client @ {dsn.rsplit('/', 1)[-1]}:{table}",
                details=details, dsn=dsn,
            )
            print(f"运行台账 → run_id={run_id}（{status}，红 {red_rows} 行，{duration_ms} ms）")
        except Exception as e:  # 写不进去要说出来：观测缺一条，不能把结论变绿
            print(f"[warn] 运行台账没写进去：{type(e).__name__}: {e}", file=sys.stderr)

    return 0 if status == "ok" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("检查脚本自身崩了（这一轮结果不可当结论）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
