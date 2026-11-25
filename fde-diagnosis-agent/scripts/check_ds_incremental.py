"""增量水位：只灌「上次之后」的新数据，而不是每晚全表重传。

四个必须回答的问题，脚本逐个验：
1. 水位存哪        -> stg_watermark 表（我方元数据，可以有 PK）
2. 怎么不重        -> 插入与水位推进同一个事务。中途崩了两者一起回滚，
                      下次从旧水位重灌；不会出现「数据进了、水位没动」导致的重复。
3. 怎么不满停      -> 水位只按「不超过当前时间」的记录推进。
                      源里一条 2027 年的脏时间如果参与取 max，水位跳到 2027，
                      之后所有真实数据都被判定为「已灌过」，增量静默停摆。
                      这条数据照常入库（B4 断言会抓它），但不许它定义水位。
4. 边界在哪        -> 水位用严格大于，同一时间戳的多条会漏。这条没解决，
                      第 [6] 步把它测出来，而不是假装没有。

用法：python scripts/check_ds_incremental.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ds.incremental import plan  # noqa: E402

DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"
STG = "stg_defect_logs"
COLS = [
    "id", "ts", "device_id", "station", "defect_type",
    "confidence", "batch", "verdict", "is_actually_ok",
]

# 源数据放内存里，不碰真表（真表有外键，插不进 CX-999）
BASE = [
    {"id": 1, "ts": "2026-11-01 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.91, "batch": "B20261101", "verdict": "NG", "is_actually_ok": False},
    {"id": 2, "ts": "2026-11-01 09:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.88, "batch": "B20261101", "verdict": "NG", "is_actually_ok": False},
    {"id": 3, "ts": "2026-11-01 10:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.75, "batch": "B20261101", "verdict": "OK", "is_actually_ok": True},
    {"id": 4, "ts": "2027-01-01 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.90, "batch": "B20261101", "verdict": "NG", "is_actually_ok": False},
    {"id": 5, "ts": "2026-11-02 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 1.50, "batch": "B20261102", "verdict": "NG", "is_actually_ok": False},
    {"id": 6, "ts": "2026-11-02 09:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": -0.10, "batch": "B20261102", "verdict": "OK", "is_actually_ok": True},
    {"id": 7, "ts": "2026-11-02 10:00:00", "device_id": "CX-101", "station": "4号线", "defect_type": "划伤", "confidence": 0.92, "batch": "B20261102", "verdict": "NG", "is_actually_ok": False},
    {"id": 8, "ts": "2026-11-03 08:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.80, "batch": "", "verdict": "OK", "is_actually_ok": True},
    {"id": 9, "ts": "2026-11-03 09:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.81, "batch": "A", "verdict": "OK", "is_actually_ok": True},
    {"id": 9001, "ts": "2026-11-04 08:00:00", "device_id": "CX-999", "station": "2号工位", "defect_type": "划伤", "confidence": 0.88, "batch": "B20261104", "verdict": "NG", "is_actually_ok": False},
]


def _ts(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def reset(conn):
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {STG}")
        cur.execute("TRUNCATE stg_watermark")
        cur.execute("TRUNCATE stg_quarantine")
    conn.commit()


def count_rows(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {STG}")
        return cur.fetchone()["n"]


def get_mark(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT mark FROM stg_watermark WHERE table_name = 'defect_logs'")
        row = cur.fetchone()
    return _ts(row["mark"]) if row and row["mark"] else None


def put_mark(conn, mark, seen):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO stg_watermark (table_name, src, mark, rows_seen, updated_at)
               VALUES ('defect_logs', 'demo', %s, %s, now())
               ON CONFLICT (table_name) DO UPDATE
               SET mark = EXCLUDED.mark,
                   rows_seen = stg_watermark.rows_seen + EXCLUDED.rows_seen,
                   updated_at = now()""",
            (mark, seen),
        )


def load(conn, rows, fail_after_insert=False):
    """返回 (本次进主表条数, 水位, 本次隔离条数)。插入与水位推进同事务。

    谁算新数据不在这里判断，交给 app.ds.incremental.plan（有单测），
    这个函数只负责把决策结果落库。
    """
    mark = get_mark(conn)
    good, bad, new_mark = plan(rows, mark)

    ph = ", ".join(["%s"] * len(COLS))
    sql = f"INSERT INTO {STG} ({', '.join(COLS)}, _src) VALUES ({ph}, 'incr')"
    with conn.cursor() as cur:
        for rec in bad:
            cur.execute(
                """INSERT INTO stg_quarantine (src_table, row_id, reason, payload)
                   VALUES ('defect_logs', %s, 'ts_in_future', %s::jsonb)
                   ON CONFLICT (src_table, row_id) DO NOTHING""",
                (rec["id"], json.dumps(rec, ensure_ascii=False)),
            )
        for rec in good:
            cur.execute(sql, [rec.get(c) for c in COLS])
        if fail_after_insert:
            raise RuntimeError("模拟装载中途崩溃")
        if good:
            put_mark(conn, new_mark, len(good))

    conn.commit()
    return len(good), new_mark, len(bad)


def main() -> int:
    extra_a = [
        {"id": 9010, "ts": "2026-11-05 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.87, "batch": "B20261105", "verdict": "NG", "is_actually_ok": False},
        {"id": 9011, "ts": "2026-11-05 09:00:00", "device_id": "CX-103", "station": "2号工位", "defect_type": "异物", "confidence": 0.66, "batch": "B20261105", "verdict": "NG", "is_actually_ok": False},
    ]
    extra_b = [
        {"id": 9020, "ts": "2026-11-06 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.83, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
    ]
    same_ts = [
        {"id": 9021, "ts": "2026-11-06 08:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.79, "batch": "B20261106", "verdict": "OK", "is_actually_ok": True},
        {"id": 9022, "ts": "2026-11-06 08:00:00", "device_id": "CX-104", "station": "2号工位", "defect_type": "划伤", "confidence": 0.85, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
    ]

    log = []
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        reset(conn)

        n, m, f = load(conn, BASE)
        log.append(("[1] 首次装载（水位为空，全量）", n, f, count_rows(conn), m))

        n, m, f = load(conn, BASE)
        log.append(("[2] 原样重跑（应 0 行）", n, f, count_rows(conn), m))

        rows3 = BASE + extra_a
        n, m, f = load(conn, rows3)
        log.append(("[3] 追加 2 条新日志", n, f, count_rows(conn), m))

        rows4 = rows3 + extra_b
        try:
            load(conn, rows4, fail_after_insert=True)
            crashed = "没崩"
        except RuntimeError:
            conn.rollback()
            crashed = "崩溃，已回滚"
        log.append((f"[4] 装载中途崩溃（{crashed}）", 0, 0, count_rows(conn), get_mark(conn)))

        n, m, f = load(conn, rows4)
        log.append(("[5] 崩溃后重跑（应只补 1 条）", n, f, count_rows(conn), m))

        n, m, f = load(conn, rows4 + same_ts)
        log.append(("[6] 同时间戳追加 2 条（预期漏）", n, f, count_rows(conn), m))

        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS total, count(DISTINCT id) AS uniq FROM {STG}")
            r = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM stg_quarantine")
            q = cur.fetchone()["n"]

    print()
    print(f"{'步骤':<32} {'主表':>4} {'隔离':>4} {'累计':>4}  水位")
    print("-" * 84)
    for name, n, f, total, mark in log:
        print(f"{name:<32} {n:>4} {f:>4} {total:>4}  {mark}")
    print("-" * 84)
    print(f"总行数 {r['total']} / 不同 id {r['uniq']}，"
          f"{'无重复' if r['total'] == r['uniq'] else '有重复'}")
    print(f"隔离区 {q} 条：重复跑不会涨（主键 + ON CONFLICT DO NOTHING）。")
    print("「隔离」那一列就是 id=4 那条 2027 年的脏数据：它没进主表，所以既不会把水位带到未来，")
    print("也不会因为「永远大于水位」而每晚被重复灌一次。")
    print("第 [6] 步是已知限制：水位用严格大于，同一秒进来的多条会被漏掉；")
    print("正确做法是水位做成 (时间戳, 主键) 复合，或留重叠窗口再按主键去重。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
