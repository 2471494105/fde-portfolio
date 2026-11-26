#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分页取数接口 + 客户端三页。

接口是第三条取数路，信号最弱的一条；客户端页面挂在同一个进程上，
不是另造一份假数据——页面上看到的每个数字，都是 ro_reader 从这个库里读出来的。
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row

try:  # uvicorn api.app:app
    from . import obs, problems
except ImportError:  # 直接 python api/app.py 的旧用法
    import obs  # type: ignore
    import problems  # type: ignore

DSN = os.environ.get(
    "FDE_CLIENT_RO_DSN",
    "postgresql://ro_reader:roreader2026pw@localhost:5432/fde_client",
)

# 集合名 -> (表名, 排序键, 可搜索列)
TABLES = {
    "devices": ("md_device", "equip_no", ("equip_no", "prod_line", "work_station", "lamp_type")),
    "defect_records": ("biz_defect_record", "rec_id", ("rec_id", "equip_no", "defect_name", "lot_no", "judge")),
    "work_orders": ("biz_work_order", "wo_no", ("wo_no", "equip_no", "station_no", "state", "stage_id")),
    "remediation_orders": (
        "gov_remediation_order",
        "rm_no",
        ("rm_no", "contract_id", "table_name", "field_name", "owner", "state"),
    ),
    "check_runs": ("gov_check_run", "ran_at", ("check_id", "status", "dataset")),
    # 中控台读的是事件，不是原始判据输出：页面不重算判据，避免出现第二套口径
    "alerts": ("gov_alert", "last_seen", ("title", "check_id", "state", "owner", "severity", "kind")),
    # 常见问题目录：客户侧的类别清单（页面用它回答「这台库一共有多少种毛病」）
    "issue_catalog": ("gov_issue_catalog", "sort_no", ("issue_id", "name", "check_id", "owner")),
    # 报警闭环那一类（判据 L）要能按 equip_no / 编号翻到原始行：明细页的「目标数据」跳这里
    "alarms": ("biz_alarm", "alarm_id", ("alarm_id", "equip_no", "code", "level")),
}

# 台账类视图按时间倒序：翻到第一页就是最近的运行，而不是最老的
DESC = {"check_runs", "alerts"}

app = FastAPI(title="Mock MES Read API", version="3.0", description="第三条取数路：分页接口 + 客户端 + 观测")


@app.middleware("http")
async def observe(request: Request, call_next):
    """每个请求记一条样本；/v1/obs 自己不记（读指标不该改指标）。"""
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    if not request.url.path.startswith("/v1/obs"):
        obs.record(request.url.path, response.status_code, ms)
    response.headers["X-Resp-Ms"] = f"{ms:.1f}"
    return response


@app.get("/healthz")
def healthz(response: Response):
    """就绪探针：能取数才算活着。分开「进程在」与「库连得上」两件事。"""
    t0 = time.perf_counter()
    try:
        with psycopg.connect(DSN) as conn:
            conn.execute("select 1").fetchone()
    except Exception as e:
        response.status_code = 503
        return {"status": "down", "reason": f"{type(e).__name__}: {e}"}
    return {"status": "ok", "db_ms": round((time.perf_counter() - t0) * 1000, 1)}


@app.get("/v1/obs/summary")
def obs_summary(response: Response):
    """三件事一次取全：接口指标 / 数据新鲜度 / 判据运行。"""
    try:
        return {
            "api": obs.api_snapshot(),
            "freshness": obs.freshness(DSN),
            "runs": obs.runs(DSN),
            "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
    except Exception as e:
        response.status_code = 503
        return {"status": "down", "reason": f"{type(e).__name__}: {e}"}


@app.get("/v1/obs/api")
def obs_api():
    return obs.api_snapshot()


@app.get("/v1/obs/freshness")
def obs_freshness():
    return obs.freshness(DSN)


@app.get("/v1/obs/runs")
def obs_runs(limit: int = Query(20, ge=1, le=200)):
    return obs.runs(DSN, limit=limit)


def jsonable(v: Any) -> Any:
    # date 与 datetime 都走这里，但 isoformat(sep=) 只认 datetime / time，
    # 纯日期列（install_dt）传 sep 会 TypeError —— 两条分支必须分开。
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return v


@app.get("/v1/{name}")
def list_rows(
    name: str,
    request: Request,
    response: Response,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=500),
    q: str = Query("", max_length=64),
    view: str = Query("full", pattern="^(full|thin)$"),
    omit_null: int = Query(0, ge=0, le=1),
):
    if name not in TABLES:
        response.status_code = 404
        return {"detail": "unknown collection: %s" % name}
    table, key, searchable = TABLES[name]
    offset = (page - 1) * size

    where, params = "", []
    if q.strip():
        clauses = ["%s::text ilike %s" % (c, "%s") for c in searchable]
        where = "where " + " or ".join(clauses)
        params = ["%%%s%%" % q.strip()] * len(searchable)

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        total = conn.execute("select count(*) as n from %s %s" % (table, where), params).fetchone()["n"]
        order = "desc" if name in DESC else "asc"
        rows = conn.execute(
            "select * from %s %s order by %s %s limit %d offset %d" % (table, where, key, order, size, offset),
            params,
        ).fetchall()

    items = []
    for r in rows:
        item = {k: jsonable(v) for k, v in r.items()}
        if view == "thin":
            item = {k: v for k, v in item.items() if k not in ("work_station", "lot_no", "is_ok")}
        if omit_null:
            item = {k: v for k, v in item.items() if v is not None}
        items.append(item)

    links = []
    base = str(request.url).split("?")[0]
    qs = "size=%d&view=%s&omit_null=%d&q=%s" % (size, view, omit_null, q)
    if page > 1:
        links.append('<%s?page=%d&%s>; rel="prev"' % (base, page - 1, qs))
    if offset + len(items) < total:
        links.append('<%s?page=%d&%s>; rel="next"' % (base, page + 1, qs))
    if links:
        response.headers["Link"] = ", ".join(links)
    response.headers["X-Total-Count"] = str(total)

    return {"page": page, "size": size, "view": view, "total": total,
            "keys": sorted(items[0].keys()) if items else [], "items": items}


@app.get("/v1/problem/{alert_id}")
def problem(alert_id: int, response: Response,
            size: int = Query(50, ge=1, le=200), page: int = Query(1, ge=1)):
    """一条事件 → 它点到的那几行。只按编号回查，不重算判据（口径在 problems.py）。"""
    try:
        res = problems.problem(DSN, alert_id, size=size, page=page)
    except Exception as e:
        response.status_code = 503
        return {"status": "down", "reason": f"{type(e).__name__}: {e}"}
    if not res:
        response.status_code = 404
        return {"detail": "no such alert: %s" % alert_id}
    return res


STATIC = Path(__file__).resolve().parent / "static"
app.mount("/app", StaticFiles(directory=str(STATIC), html=True), name="app")


@app.get("/console", include_in_schema=False)
def console():
    """中控台与作业页同一个进程、同一个端口、同一套只读账号：不另开服务。"""
    from fastapi.responses import FileResponse

    return FileResponse(STATIC / "console.html")


@app.get("/problem", include_in_schema=False)
def problem_page():
    """明细页：从中控台上点一条事件进来，看到具体哪几行、值的原样、该改哪一栏。"""
    from fastapi.responses import FileResponse

    return FileResponse(STATIC / "problem.html")