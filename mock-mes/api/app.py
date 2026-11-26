#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分页取数接口 + 客户端三页。

接口是第三条取数路，信号最弱的一条；客户端页面挂在同一个进程上，
不是另造一份假数据——页面上看到的每个数字，都是 ro_reader 从这个库里读出来的。
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row

DSN = os.environ.get(
    "FDE_CLIENT_RO_DSN",
    "postgresql://ro_reader:roreader2026pw@localhost:5432/fde_client",
)

# 集合名 -> (表名, 排序键, 可搜索列)
TABLES = {
    "devices": ("md_device", "equip_no", ("equip_no", "prod_line", "work_station", "lamp_type")),
    "defect_records": ("biz_defect_record", "rec_id", ("equip_no", "defect_name", "lot_no", "judge")),
    "work_orders": ("biz_work_order", "wo_no", ("wo_no", "equip_no", "station_no", "state", "stage_id")),
    "remediation_orders": (
        "gov_remediation_order",
        "rm_no",
        ("rm_no", "contract_id", "table_name", "field_name", "owner", "state"),
    ),
}

app = FastAPI(title="Mock MES Read API", version="2.0", description="第三条取数路：分页接口 + 客户端")


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
        rows = conn.execute(
            "select * from %s %s order by %s limit %d offset %d" % (table, where, key, size, offset),
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


STATIC = Path(__file__).resolve().parent / "static"
app.mount("/app", StaticFiles(directory=str(STATIC), html=True), name="app")