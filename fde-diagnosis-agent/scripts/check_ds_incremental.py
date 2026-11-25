"""增量水位：只灌「上次之后」的新数据，而不是每晚全表重传。

五个必须回答的问题，脚本逐个验：
1. 水位存哪        -> stg_watermark 表（我方元数据，可以有 PK）
2. 怎么不重        -> 两道防线：插入与水位推进同一个事务（崩了一起重滚）；
                      装载前按主键过滤已存在的行（幂等，客户重传整批也不重复）
3. 怎么不满停      -> 水位只按「不超过当前时间」的记录推进。
                      源里一条 2027 年的脏时间如果参与取 max，水位跳到 2027，
                      之后所有真实数据都被判定为「已灌过」，增量静默停摆。
                      这条数据照常进隔离区（B4 断言会抓它），但不许它定义水位。
4. 同时间戳怎么办  -> 水位做成 (时间戳, 主键) 复合，同一秒进来的多条只要有更大的
                      主键就算新。第 [6] 步验证它从「漏 2 条」变成「补 2 条」。
5. 边界在哪        -> 复合水位要求主键单调递增。客户重传一条更小的主键（补录旧单）
                      会漏，第 [8] 步把这条残留边界测出来，而不是假装没有。

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


def fmt_mark(mark):
    """水位统一显示成 `时间戳 #主键`，避免第一行是 int、第二行是 str 这类不一致。"""
    if mark is None:
        return "（空）"
    if isinstance(mark, tuple):
        return f"{mark[0]:%Y-%m-%d %H:%M:%S} #{mark[1]}"
    return f"{mark:%Y-%m-%d %H:%M:%S} #（无主键）"


def get_mark(conn):
    """返回 (时间戳, 主键) 或 None。主键为空时退化成只比时间戳。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mark, mark_pk FROM stg_watermark WHERE table_name = 'defect_logs'"
        )
        row = cur.fetchone()
    if not row or not row["mark"]:
        return None
    return (_ts(row["mark"]), row["mark_pk"])


def put_mark(conn, mark, seen):
    """mark 是 (时间戳, 主键) 二元组。"""
    ts, pk = mark
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO stg_watermark (table_name, src, mark, mark_pk, rows_seen, updated_at)
               VALUES ('defect_logs', 'demo', %s, %s, %s, now())
               ON CONFLICT (table_name) DO UPDATE
               SET mark = EXCLUDED.mark,
                   mark_pk = EXCLUDED.mark_pk,
                   rows_seen = stg_watermark.rows_seen + EXCLUDED.rows_seen,
                   updated_at = now()""",
            (ts, None if pk is None else str(pk), seen),
        )


def load(conn, rows, fail_after_insert=False):
    """返回 (本次新增条数, 水位, 本次隔离条数)。插入与水位推进同事务。

    谁算新数据交给 app.ds.incremental.plan（有单测）；这里补上 plan 不知道的一半：
    主表里已经有什么。plan 只按水位判断，客户重传整批时会再吐一遍已灌过的行，
    所以装载前按主键过滤。这就是「plan 负责谁该看，落库负责谁是新的」。
    """
    mark = get_mark(conn)
    good, bad, new_mark = plan(rows, mark)

    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {STG}")
        existing = {r["id"] for r in cur.fetchall()}

    fresh = [rec for rec in good if rec["id"] not in existing]

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
        for rec in fresh:
            cur.execute(sql, [rec.get(c) for c in COLS])
        if fail_after_insert:
            raise RuntimeError("模拟装载中途崩溃")
        # 只要 plan 认定有新行就推进水位——哪怕这些行全在主表里（已灌过）。
        # 否则「整批重传」会让水位原地不动，下次还要重读同一段。
        if good:
            put_mark(conn, new_mark, len(fresh))

    conn.commit()
    return len(fresh), new_mark, len(bad)


def main() -> int:
    extra_a = [
        {"id": 9010, "ts": "2026-11-05 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.87, "batch": "B20261105", "verdict": "NG", "is_actually_ok": False},
        {"id": 9011, "ts": "2026-11-05 09:00:00", "device_id": "CX-103", "station": "2号工位", "defect_type": "异物", "confidence": 0.66, "batch": "B20261105", "verdict": "NG", "is_actually_ok": False},
    ]
    extra_b = [
        {"id": 9020, "ts": "2026-11-06 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.83, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
    ]
    # 与 extra_b 同一时间戳、主键更大的两条：复合水位要能认出来
    same_ts = [
        {"id": 9021, "ts": "2026-11-06 08:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.79, "batch": "B20261106", "verdict": "OK", "is_actually_ok": True},
        {"id": 9022, "ts": "2026-11-06 08:00:00", "device_id": "CX-104", "station": "2号工位", "defect_type": "划伤", "confidence": 0.85, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
    ]
    # 同一时间戳、主键更小的补录单：复合水位认不出来，这是它的残留边界
    late_small = [
        {"id": 9019, "ts": "2026-11-06 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.84, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
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
        log.append(("[6] 同时间戳 + 更大主键（应补 2 条）", n, f, count_rows(conn), m))

        n, m, f = load(conn, rows4 + same_ts)
        log.append(("[7] 原样重跑（整批重传，应 0 条且不重复）", n, f, count_rows(conn), m))

        n, m, f = load(conn, rows4 + same_ts + late_small)
        log.append(("[8] 同时间戳 + 更小主键（残留边界，预期漏 1）", n, f, count_rows(conn), m))

        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS total, count(DISTINCT id) AS uniq FROM {STG}")
            r = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM stg_quarantine")
            q = cur.fetchone()["n"]

    print()
    print(f"{'步骤':<40} {'新增':>4} {'隔离':>4} {'累计':>4}  水位")
    print("-" * 100)
    for name, n, f, total, mark in log:
        print(f"{name:<40} {n:>4} {f:>4} {total:>4}  {fmt_mark(mark)}")
    print("-" * 100)
    print(f"总行数 {r['total']} / 不同 id {r['uniq']}，"
          f"{'无重复' if r['total'] == r['uniq'] else '有重复'}")
    print(f"隔离区 {q} 条：重复跑不会涨（主键 + ON CONFLICT DO NOTHING）。")
    print("「隔离」那一列就是 id=4 那条 2027 年的脏数据：它没进主表，所以既不会把水位带到未来，")
    print("也不会因为「永远大于水位」而每晚被重复灌一次。")
    print("第 [6] 步是这次修的：水位改成 (时间戳, 主键) 复合后，同一秒进来的多条不再被整体漏掉。")
    print("第 [8] 步是残留边界：复合水位要求主键单调递增，客户补录一条更小的主键仍会漏。")
    print("真正能覆盖它的做法是留重叠窗口（水位往回退一段）再按主键去重，成本是每次多读一小段。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
