"""判据 L：报警有没有人接、清了有没有人销（biz_alarm）。

为什么补这一条
--------------
现有三类问题（归属、字段、隔离区）都在说「数据填得对不对」。客户真正会指着屏幕
讲的另一件事是「这条报警挂了三天没人管」——`biz_alarm` 120 行里有 40 行
`acked_by` 与 `cleared_at` **同时为空**：既没人认领，也没有关闭时刻。这不是脏
数据，是**流程没有闭环**，而且它和工位无关（这张表连 station_id 都没有），
拿归属类去套只会得出「没法判」的结论。

判定口径（两列分开，因为修复动作不同）
- `acked_by` 空  → 有人接了吗？没接 = 责任没落地；
- `cleared_at` 空 → 处理完没写？= 闭环断了。
两列都空的行只算一次（按 alarm_id 去重，`red_metric=violating_rows`），但台账会
分别记下 `acked_missing` / `cleared_missing` 的编号集合，明细页据此说清「缺哪一列」。

编号格式 `alarm:<alarm_id>`：与归属类的 `wo_station_identity#WO-...` 同一套约定
（前缀 + 定位键），明细页按前缀找目标表，不重算判据。

用法：
    python scripts/check_alarm_ack.py
    python scripts/check_alarm_ack.py --json=eval/alarm_ack.json
    python scripts/check_alarm_ack.py --no-record
    python scripts/check_alarm_ack.py --dsn=... --table=probe.alarm   # 反例用
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import runlog  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
DEFAULT_TABLE = "biz_alarm"
COLS = ["alarm_id", "equip_no", "raised_at", "code", "level", "acked_by", "cleared_at"]


def _blank(v) -> bool:
    """NULL 与空串在客户眼里都是「没写」。别把它们合并成 0。"""
    return v is None or (isinstance(v, str) and v.strip() == "")


def load_rows(dsn: str, table: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLS)} FROM {table} ORDER BY alarm_id")
        return [dict(r) for r in cur.fetchall()]


def judge(rows: list[dict]) -> dict:
    acked_missing = [r for r in rows if _blank(r["acked_by"])]
    cleared_missing = [r for r in rows if _blank(r["cleared_at"])]
    red = {r["alarm_id"] for r in acked_missing} | {r["alarm_id"] for r in cleared_missing}
    both = {r["alarm_id"] for r in acked_missing} & {r["alarm_id"] for r in cleared_missing}
    return {
        "acked_missing": sorted({r["alarm_id"] for r in acked_missing}),
        "cleared_missing": sorted({r["alarm_id"] for r in cleared_missing}),
        "both_missing": sorted(both),
        "red_ids": [f"alarm:{i}" for i in sorted(red)],
        "red_rows": len(red),
    }


def main(argv: list[str]) -> int:
    dsn, table, json_out = CLIENT_DSN, DEFAULT_TABLE, None
    record_run = True
    for a in argv:
        if a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--table="):
            table = a.split("=", 1)[1]
        elif a.startswith("--json="):
            json_out = a.split("=", 1)[1]
        elif a == "--no-record":
            record_run = False
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    t0 = time.perf_counter()
    try:
        rows = load_rows(dsn, table)
    except Exception as e:
        print(f"[error] 取数失败（{table} @ {dsn}）：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    res = judge(rows)
    print(f"观测面 {table} @ {dsn.rsplit('/', 1)[-1]}，{len(rows)} 行")
    print(f"[{'FAIL' if res['red_rows'] else 'OK':4s}] 报警闭环：{res['red_rows']} 行没有闭环"
          f"（没人接 {len(res['acked_missing'])} · 没写关闭 {len(res['cleared_missing'])}"
          f" · 两列都空 {len(res['both_missing'])}）")

    status = "fail" if res["red_rows"] else "ok"
    summary = {"table": table, "rows": len(rows), "status": status,
               "red_rows": res["red_rows"], "red_ids": res["red_ids"],
               "acked_missing": res["acked_missing"], "cleared_missing": res["cleared_missing"],
               "both_missing": res["both_missing"]}

    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json → {out}")

    if record_run:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        details = {
            "red_metric": "violating_rows",
            "table": table,
            "acked_missing": res["acked_missing"],
            "cleared_missing": res["cleared_missing"],
            "both_missing": res["both_missing"],
            "ids": res["red_ids"],
        }
        try:
            run_id = runlog.record("alarm_ack", status, res["red_rows"], duration_ms,
                                   dataset=f"fde_client @ {dsn.rsplit('/', 1)[-1]}:{table}",
                                   details=details, dsn=dsn)
            print(f"运行台账 → run_id={run_id}（{status}，红 {res['red_rows']} 行，{duration_ms} ms）")
        except Exception as e:
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
