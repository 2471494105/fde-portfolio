"""数据源抽象。

全项目对业务库的读取都收束在 app/tools/db.py:connect() 一个点上。这里把那一步
抽成可替换的接口：换库、换形态（CSV / 外部 SQL / HTTP 网关）时只需实现一个新的
DataSource，调用方一行不用改。

一条硬约定：connect() 返回的对象必须兼容 psycopg 的用法，即

    with src.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cur.fetchall()          # row_factory=dict_row，返回 list[dict]

因为现存 5 处工具都是这么写的（diagnosis.py / manual.py / workorder.py），
其中三份在 BEHAVIOR_FILES 里。改动这个语义就等于换芯，会改变 code_hash，
不属于「加壳」阶段该做的事。
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataSource(Protocol):
    """一个能提供连接、并报告自身能力的数据源。"""

    name: str

    def connect(self) -> Any:
        """返回可用连接。语义与 psycopg.connect(dsn, row_factory=dict_row) 一致。"""
        ...

    def health(self) -> dict:
        """探测可用性。失败不抛异常，返回 {'ok': False, 'reason': ...}。

        调用方（健康检查、验收脚本）靠它区分「连不上」和「查不到」，
        而不是靠捕获异常猜。
        """
        ...

    def capabilities(self) -> list:
        """这个源支持什么，例如 ['sql', 'read_only']。

        用于接线时判断某个工具能否落到这个源上；
        不写死的字符串，是因为后面要按能力挑源，而不是按名字。
        """
        ...
