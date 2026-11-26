"""判据运行台账：每次跑判据落一行，供观测页与趋势使用。

为什么要落库而不是打印到终端
--------------------------
判据红一次是屏幕上的十几行字，下一次运行就把它覆盖了。没有台账时，
「昨天是不是红的」「这道判据多久没跑过」只能靠人回忆——而可观测性最先
要回答的恰恰是「这件事发生过吗、什么时候、结果如何」。

写入失败不许静默
----------------
`record()` 写不进去会抛异常，调用方要把这件事**说出来**（打 stderr 并继续
跑判据）：观测缺一条不能把结论变绿，但也不能假装写成功了。页面那头还有
一层兜底——最近一次运行超过 24 小时就报红，监控自己静默比数据静默更危险。
"""

from __future__ import annotations

import json
from datetime import datetime

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"


def record(
    check_id: str,
    status: str,
    red_count: int,
    duration_ms: int,
    dataset: str | None = None,
    details: dict | None = None,
    dsn: str = CLIENT_DSN,
    code_hash: str | None = None,
) -> int:
    """写一行运行记录，返回 run_id。status 只认 ok / warn / fail / error。"""
    if status not in ("ok", "warn", "fail", "error"):
        raise ValueError(f"status 只认 ok/warn/fail/error，收到 {status!r}")
    if red_count < 0:
        raise ValueError("red_count 不能为负")
    if code_hash is None:
        try:
            from app import meta

            code_hash = meta.code_hash()
        except Exception:
            code_hash = None

    import psycopg

    payload = json.dumps(details or {}, ensure_ascii=False)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gov_check_run (check_id, duration_ms, status, red_count, dataset, code_hash, details)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING run_id",
            (check_id, int(duration_ms), status, int(red_count), dataset, code_hash, payload),
        )
        run_id = cur.fetchone()[0]
    return int(run_id)


def latest(check_id: str, dsn: str = CLIENT_DSN) -> dict | None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, check_id, ran_at, duration_ms, status, red_count, dataset, code_hash"
            " FROM gov_check_run WHERE check_id = %s ORDER BY ran_at DESC LIMIT 1",
            (check_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def staleness_hours(check_id: str, dsn: str = CLIENT_DSN, now: datetime | None = None) -> float | None:
    """距最近一次运行多少小时；从没跑过返回 None（调用方要当红处理，不是当 0）。"""
    row = latest(check_id, dsn)
    if not row:
        return None
    now = now or datetime.now(row["ran_at"].tzinfo)
    return (now - row["ran_at"]).total_seconds() / 3600.0
