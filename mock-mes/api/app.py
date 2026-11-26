#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分页取数接口——第三条路，信号最弱的一条。

三个刻意的设定：
  1) 列集合随视图变（?view=thin 少三列）——真实系统的列表页和详情页字段本来就不同。
  2) ?omit_null=1 走"按行只输出非空字段"这种真实序列化，键集合会随行变。
  3) 只给 ro_reader 的连接串：接口能看到的，和只读账号能看到的一样多。
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

import psycopg
from fastapi import FastAPI, Query, Request, Response
from psycopg.rows import dict_row

DSN = os.environ.get(
    "FDE_CLIENT_RO_DSN",
    "postgresql://ro_reader:roreader2026pw@localhost:5432/fde_client",
)

TABLES = {
    "defect_records": ("biz_defect_record", "rec_id"),
    "devices": ("md_device", "equip_no"),
    "work_orders": ("biz_work_order", "wo_no"),
}

THIN_DROP = ("work_station", "lot_no", "is_ok")

app = FastAPI(title="Mock MES Read API", version="1.0", description="第三条取数路：分页接口")


def jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ")
    return v


@app.get("/v1/{name}")
def list_rows(
    name: str,
    request: Request,
    response: Response,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    view: str = Query("full", pattern="^(full|thin)$"),
    omit_null: int = Query(0, ge=0, le=1),
):
    if name not in TABLES:
        response.status_code = 404
        return {"detail": "unknown collection: %s" % name}
    table, key = TABLES[name]
    offset = (page - 1) * size

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        total = conn.execute("select count(*) as n from %s" % table).fetchone()["n"]
        rows = conn.execute(
            "select * from %s order by %s limit %d offset %d" % (table, key, size, offset)
        ).fetchall()

    items = []
    for r in rows:
        item = {k: jsonable(v) for k, v in r.items()}
        if view == "thin":
            item = {k: v for k, v in item.items() if k not in THIN_DROP}
        if omit_null:
            item = {k: v for k, v in item.items() if v is not None}
        items.append(item)

    links = []
    base = str(request.url).split("?")[0]
    qs = "size=%d&view=%s&omit_null=%d" % (size, view, omit_null)
    if page > 1:
        links.append('<%s?page=%d&%s>; rel="prev"' % (base, page - 1, qs))
    if offset + len(items) < total:
        links.append('<%s?page=%d&%s>; rel="next"' % (base, page + 1, qs))
    if links:
        response.headers["Link"] = ", ".join(links)
    response.headers["X-Total-Count"] = str(total)

    return {
        "page": page,
        "size": size,
        "view": view,
        "total": total,
        "keys": sorted(items[0].keys()) if items else [],
        "items": items,
    }