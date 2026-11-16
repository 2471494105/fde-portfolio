"""数据库连接。项目二与项目一共库（fde_rag），但表分离。"""

import os

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("FDE_PG_DSN", "postgresql://postgres:postgres@localhost:5432/fde_rag")


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)
