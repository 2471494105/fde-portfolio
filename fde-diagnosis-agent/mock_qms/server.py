"""Mock QMS 工单服务。

刻意复现一个真实集成陷阱：建单失败时 HTTP 仍然返回 200，
错误码放在响应体的 code 字段里。调用方只看 HTTP 状态码就会把失败当成功，
这是企业系统集成里最常见、也最难在测试里暴露的一类问题。

真实系统里常见的原因：网关/负载均衡对所有业务响应统一包一层，
后端抛业务异常时由过滤器兜住并返回 200 + 业务码。
"""

import hashlib
from datetime import datetime

import psycopg
from fastapi import FastAPI
from psycopg.rows import dict_row
from pydantic import BaseModel

DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"

SEVERITY_GROUP = {
    "high": "设备科",
    "medium": "工艺/算法组",
    "low": "班组自处理",
}

app = FastAPI(title="Mock QMS", version="1.0")


def _conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def _resp(code=0, message="ok", data=None):
    return {"code": code, "message": message, "data": data}


class WorkOrderIn(BaseModel):
    device_id: str
    root_cause: str
    root_cause_code: str = "other"
    severity: str
    actions: list[str]
    evidence_refs: list[str]
    idempotency_key: str | None = None


def _idem_key(device_id: str, root_cause_code: str) -> str:
    """幂等键 = f(设备, 故障分类)，**与日期无关**。

    两个刻意的取舍：

    1. 不含自然语言根因。用措辞做 hash，键就跟着措辞漂——"光源超额定寿命导致
       亮度衰减" 和 "光源超寿命" 是同一件事却各建一张单。分类码是枚举，稳定。
    2. 不含日期。工单的生命周期是「待处理 → 处理中 → 已完成」，不是"一天"。
       键里带日期，就等于宣布"昨天的同一件事今天可以再建一张"——
       现场表现是同一个坏光源连着三天三张待处理单，设备科不知道该处理哪张，
       而数据库认为三张都合法。去重的语义是"这件事还挂着没办完"，
       这件事挂多久跟日历无关。
    """
    raw = f"{device_id}|{root_cause_code}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@app.get("/health")
def health():
    return _resp(0, "ok", {"service": "mock-qms"})


@app.post("/api/work-order")
def create_work_order(body: WorkOrderIn):
    # 业务校验失败也返回 200，错误码在 code 里
    if body.severity not in SEVERITY_GROUP:
        return _resp(4001, f"severity 不合法：{body.severity}，应为 {sorted(SEVERITY_GROUP)}")

    if not body.actions:
        return _resp(4002, "处置动作不能为空")

    day = datetime.now().strftime("%Y-%m-%d")
    # day 只用来给单号打日期戳，不进幂等键
    key = body.idempotency_key or _idem_key(body.device_id, body.root_cause_code)

    with _conn() as conn, conn.cursor() as cur:
        # 只跟"还没关闭"的单去重：处理完的单不再占位，同一问题再坏可以重新建单。
        # 关闭状态用黑名单列出，与 work_orders_open_key 索引的 WHERE 条件必须一致——
        # 两处不一致就会出现"查询说不存在、插入却撞唯一约束"的鬼故事。
        cur.execute(
            "SELECT * FROM work_orders WHERE idempotency_key = %s "
            "AND status NOT IN ('已完成', '已合并', '已取消')",
            (key,),
        )
        existed = cur.fetchone()
        if existed:
            return _resp(0, "ok", {
                "order_id": existed["order_id"],
                "status": existed["status"],
                "deduped": True,
                "assignee_group": existed["assignee_group"],
            })

        cur.execute("SELECT count(*) AS n FROM work_orders")
        seq = cur.fetchone()["n"] + 1
        order_id = f"WO-{day.replace('-', '')}-{seq:03d}"

        cur.execute("""
            INSERT INTO work_orders
              (order_id, device_id, root_cause, root_cause_code, severity, actions,
               evidence_refs, idempotency_key, status, assignee_group)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
        """, (
            order_id, body.device_id, body.root_cause, body.root_cause_code, body.severity,
            __import__("json").dumps(body.actions, ensure_ascii=False),
            __import__("json").dumps(body.evidence_refs, ensure_ascii=False),
            key, "待处理", SEVERITY_GROUP[body.severity],
        ))
        conn.commit()

    return _resp(0, "ok", {
        "order_id": order_id,
        "status": "待处理",
        "deduped": False,
        "assignee_group": SEVERITY_GROUP[body.severity],
    })


@app.get("/api/work-order/{order_id}")
def get_work_order(order_id: str):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM work_orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
    if not row:
        return _resp(4041, f"工单 {order_id} 不存在")
    row["actions"] = row["actions"] or []
    row["evidence_refs"] = row["evidence_refs"] or []
    row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
    return _resp(0, "ok", row)
