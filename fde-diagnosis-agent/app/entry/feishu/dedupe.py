"""入口判重：同一个 message_id 只处理一次。

为什么不用内存 set：飞书事件重推窗口是 7.1 小时，进程一重启内存就空了；
放 PG 里才能做到"重启还记得"。TTL 给 24 小时，比官方窗口宽一截。

为什么只在入口挡一层：入口键（message_id）防的是"同一条事件投两次"，
业务键（sha256(device|root_cause)）防的是"两句话建了两张单"——
这是两种故障模式，不是冗余（DOC-36 §五）。
"""

import os

import psycopg

DSN = os.getenv("FDE_PG_DSN", "postgresql://postgres:postgres@localhost:5432/fde_rag")
RETENTION_HOURS = 24

DDL = """
CREATE TABLE IF NOT EXISTS entry_dedupe (
    channel    text        NOT NULL,
    event_key  text        NOT NULL,
    expire_at  timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, event_key)
)
"""

# 一条语句完成"查 + 占位"，不给两次请求之间留窗口
CLAIM = """
INSERT INTO entry_dedupe (channel, event_key, expire_at)
VALUES (%s, %s, now() + make_interval(hours => %s))
ON CONFLICT (channel, event_key) DO NOTHING
RETURNING event_key
"""


class Dedupe:
    """短连接：一个事件开一次。

    演示量级是每分钟几条，维护连接池反而是新的故障源（断线、失活检测）；
    这个进程真正长连的是飞书那条 WebSocket。
    """

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or DSN

    def _connect(self):
        return psycopg.connect(self._dsn)

    def setup(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(DDL)
            conn.commit()

    def seen(self, channel: str, event_key: str) -> bool:
        """True = 这条事件之前处理过。第一次看到返回 False。

        命中是飞书的正常重推，不是错误：不抛异常、不回执失败、只丢掉。
        插不进去（ON CONFLICT DO NOTHING 后没有 RETURNING 行）就说明已经有人占过这个键。
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(CLAIM, (channel, event_key, RETENTION_HOURS))
            fresh = cur.fetchone() is not None
            conn.commit()
        return not fresh

    def prune(self) -> int:
        """删过期行。放在启动时做一次就够，不必再挂定时器。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM entry_dedupe WHERE expire_at < now()")
            n = cur.rowcount
            conn.commit()
        return n
