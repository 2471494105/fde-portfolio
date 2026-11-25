"""PostgreSQL 数据源：当前唯一实现。

内容就是从原 app/tools/db.py 搬过来的两行（DSN + psycopg.connect）。
行为刻意保持逐字节等价：同样的 DSN 来源、同样的 row_factory=dict_row。
"""

import os
from typing import Optional

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("FDE_PG_DSN", "postgresql://postgres:postgres@localhost:5432/fde_rag")


class PgSource:
    name = "pg"

    def __init__(self, dsn: Optional[str] = None) -> None:
        self.dsn = dsn or DSN

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def health(self) -> dict:
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
            return {"ok": True, "name": self.name}
        except Exception as exc:  # 健康探测不该把调用方炸掉
            return {"ok": False, "name": self.name, "reason": str(exc)}

    def capabilities(self) -> list:
        # read_only：本项目的读工具只做 select；写工单走 QMS HTTP，不走这里。
        return ["sql", "read_only"]
