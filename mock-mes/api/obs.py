"""接口自测指标 + 数据新鲜度 + 判据运行台账：可观测性的三件事。

为什么这三件
------------
「可观测」常被做成一张好看的图，但对这套系统，真正要回答的是三个问题：
  1. 这个接口现在正常吗、慢不慢（`/v1/obs/api`：近 300 次请求的 p50/p95、错误数、最慢几条）；
  2. 库里的数据有多旧（`/v1/obs/freshness`：每张业务表最近一条事件时间，超过 24 小时算 stale）；
  3. 判据到底跑过没有（`/v1/obs/runs`：最近 20 次运行，外加「多久没跑」的告警）。

第 3 条是这套系统特有的：**监控自己静默，比数据静默更危险**。页面若发现某条
判据超过 24 小时没运行，会标红，而不是继续显示上一次的绿。

指标是进程内内存（`deque(maxlen=300)`），重启即归零——这是有意的：它不是
长期存储，长期的事实（运行台账）落在 `gov_check_run` 里。
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from datetime import datetime, timezone

WINDOW = 300
_STALE_HOURS = 24

_lock = threading.Lock()
_samples: deque[tuple[datetime, str, int, float]] = deque(maxlen=WINDOW)
_paths: Counter = Counter()
_errors: Counter = Counter()

# 业务表 → 事件时间列（None = 静态主数据，没有「最新」这回事）
FRESHNESS = [
    ("biz_work_order", "opened_at", "业务"),
    ("biz_defect_record", "captured_at", "业务"),
    ("biz_inspection_lot", "started_at", "业务"),
    ("biz_alarm", "raised_at", "业务"),
    ("biz_repair_order", "opened_at", "业务"),
    ("gov_remediation_order", "created_at", "治理"),
    ("gov_check_run", "ran_at", "观测"),
    ("md_device", None, "主数据"),
    ("md_station", None, "主数据"),
]


def record(path: str, status: int, ms: float) -> None:
    with _lock:
        _samples.append((datetime.now(timezone.utc), path, int(status), float(ms)))
        _paths[path] += 1
        if status >= 400:
            _errors[path] += 1


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * q))))
    return values[idx]


def api_snapshot() -> dict:
    with _lock:
        rows = list(_samples)
    lat = [r[3] for r in rows]
    errors = sum(1 for r in rows if r[2] >= 400)
    slow = sorted(rows, key=lambda r: r[3], reverse=True)[:5]
    return {
        "window": WINDOW,
        "total_since_start": sum(_paths.values()),
        "in_window": len(rows),
        "p50_ms": round(_pct(lat, 0.5), 1),
        "p95_ms": round(_pct(lat, 0.95), 1),
        "max_ms": round(max(lat), 1) if lat else 0.0,
        "errors": errors,
        "by_status": dict(Counter("2xx" if r[2] < 300 else "3xx" if r[2] < 400 else "4xx" if r[2] < 500 else "5xx" for r in rows)),
        "slowest": [
            {"path": r[1], "status": r[2], "ms": round(r[3], 1), "at": r[0].isoformat()}
            for r in slow
        ],
        "top_paths": _paths.most_common(6),
    }


def freshness(dsn: str) -> dict:
    """每张表：行数、最近事件、距今小时、是否 stale（> 24h）。"""
    import psycopg

    out = []
    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn:
        for table, tcol, kind in FRESHNESS:
            rows = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()[0]
            latest = None
            if tcol:
                latest = conn.execute(f"SELECT max({tcol}) AS t FROM {table}").fetchone()[0]
            age_h = None
            stale = False
            future = False
            if latest is not None:
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                age_h = round((now - latest).total_seconds() / 3600.0, 1)
                stale = age_h > _STALE_HOURS
                future = age_h < 0
            out.append(
                {
                    "table": table,
                    "kind": kind,
                    "rows": rows,
                    "latest": latest.isoformat(sep=" ") if latest else None,
                    "age_hours": age_h,
                    "stale": stale,
                    "future": future,
                }
            )
    stale_tables = [r["table"] for r in out if r["stale"]]
    future_tables = [r["table"] for r in out if r["future"]]
    return {
        "threshold_hours": _STALE_HOURS,
        "tables": out,
        "stale": stale_tables,
        "future": future_tables,
    }


def runs(dsn: str, limit: int = 20) -> dict:
    """最近 limit 次判据运行 + 每条判据「多久没跑」（从没跑过记 inf）。"""
    import psycopg
    from psycopg.rows import dict_row

    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, check_id, ran_at, duration_ms, status, red_count, dataset"
            " FROM gov_check_run ORDER BY ran_at DESC LIMIT %s",
            (limit,),
        )
        recent = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT check_id, max(ran_at) AS last_at, count(*) AS n"
            " FROM gov_check_run GROUP BY check_id ORDER BY check_id"
        )
        per = [dict(r) for r in cur.fetchall()]

    checks = []
    for p in per:
        last = p["last_at"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        hours = round((now - last).total_seconds() / 3600.0, 1)
        checks.append(
            {
                "check_id": p["check_id"],
                "runs": p["n"],
                "last_at": last.isoformat(sep=" "),
                "hours_since": hours,
                "silent": hours > _STALE_HOURS,
            }
        )

    for r in recent:
        if r["ran_at"].tzinfo is None:
            r["ran_at"] = r["ran_at"].replace(tzinfo=timezone.utc)
        r["ran_at"] = r["ran_at"].isoformat(sep=" ")
        r["minutes_since"] = round((now - datetime.fromisoformat(r["ran_at"])).total_seconds() / 60.0, 1)

    return {"recent": recent, "checks": checks, "threshold_hours": _STALE_HOURS}
