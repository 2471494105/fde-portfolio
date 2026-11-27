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

import json
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

WINDOW = 300
_STALE_HOURS = 24

_lock = threading.Lock()
_samples: deque[tuple[datetime, str, int, float]] = deque(maxlen=WINDOW)
_paths: Counter = Counter()
_errors: Counter = Counter()

# 业务表 → 事件时间列，只认一处事实：fde-diagnosis-agent 的 freshness_map.json。
# 判据（scripts/check_freshness.py）读的是同一份——页面绿、判据红这种「两套口径」不允许再出现。
# 读取失败**不兜底**：宁可 500，也不静默用内置清单，那会悄悄变回第二套口径。
FRESH_MAP = (Path(__file__).resolve().parents[2] / "fde-diagnosis-agent"
             / "data" / "mapping" / "freshness_map.json")

try:  # uvicorn api.app:app
    from .rules import status_of
except ImportError:  # 直接 python api/app.py 的旧用法
    from rules import status_of  # type: ignore


def load_freshness_spec(path: Path = FRESH_MAP) -> tuple[float, list[tuple[str, str | None, str, bool]]]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        tables = [(t["table"], t.get("time_col"), t.get("kind", ""), bool(t.get("static")))
                  for t in spec["tables"]]
        return float(spec.get("default_max_age_hours", 24)), tables
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"新鲜度登记读取失败（{path}）：{type(e).__name__}: {e}") from e


def record(path: str, status: int, ms: float) -> None:
    # favicon 是浏览器自己来要的，404 不代表接口坏了；混进错误率会让人去查一条不存在的路
    if path.rstrip("/").endswith("/favicon.ico"):
        return
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
        # 错误数单独一个 31 说明不了「哪条路在坏」：按路径拆，页面才能指向一条接口
        "errors_by_path": [{"path": p, "n": n} for p, n in _errors.most_common(5)],
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

    threshold_h, tables = load_freshness_spec()
    out = []
    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn:
        for table, tcol, kind, is_static in tables:
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
                stale = age_h > threshold_h
                future = age_h < 0
            out.append(
                {
                    "table": table,
                    "kind": kind,
                    "static": is_static,
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
        "threshold_hours": threshold_h,
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
        # 这一行红是「数据本身错」还是「我们自己的规矩」：页面据此决定要不要计入
        # 「确定违约」。登记读不到会抛（见 rules.load），不在这里兜底成 hard。
        rule = status_of(r["check_id"])
        r["rule_status"] = rule.get("status")
        r["rule_unit"] = rule.get("unit")
        r["counts_in_total"] = bool(rule.get("counts_in_total"))
        r["row_kpi"] = bool(rule.get("row_kpi"))
        r["rule_basis"] = rule.get("basis")
        # 题面（问客户的原话 + 答完会怎样）也走同一份登记：
        # 页面「等客户回答的几道题」直接从这里取，避免文案在页面里再抄一遍。
        r["rule_ask"] = rule.get("ask")
        r["rule_source"] = rule.get("source")

    return {"recent": recent, "checks": checks, "threshold_hours": _STALE_HOURS}
