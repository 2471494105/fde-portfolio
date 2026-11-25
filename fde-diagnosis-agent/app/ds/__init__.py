"""数据源注册与选择。

两级，各有各的协议，互不冒充：

- 游标源（CursorSource）：能给 psycopg 兼容连接，db.py 用。当前只有 pg。
- 行源（RowSource）：只能按主键取一行，用于对账、补数、单表替换。当前只有 csv。

为什么不合成一个：能跑 SQL 和只能取一行是两种能力，不是一个东西的两种实现。
合起来就会写出「有 connect 但没有 cursor」的对象，工具一调就炸。

选谁由环境变量决定，代码里不出现 if/else：
  FDE_DS      = pg    → get_source()
  FDE_ROW_DS  = csv   → get_row_source()

set_* / reset_* 是给测试用的：没有这两个钩子，验证「工具确实走了接口」
就只能连真库，慢且不可控。

登记规则（留给以后的人）：
本目录当前**不参与** code_hash，因为它还换不掉任何输出——只是壳。
哪天某个源真的接管了某个工具的取数，那一次提交必须同时做三件事：
1) 改行为；2) 把 app/ds/* 加进 app/meta.py 的 BEHAVIOR_FILES；
3) 重跑全量并登记新基线。
只做第 1 件，就是本项目已经踩过的那个坑：改了行为，指纹不动，报告还在
声称「与旧基线可比」。
"""

import os
from typing import Optional

from app.ds.base import DataSource
from app.ds.csv import CsvSource
from app.ds.pg import PgSource
from app.ds.rows import RowSource

_CURSOR_SOURCES = {
    "pg": PgSource,
}

_ROW_SOURCES = {
    "csv": CsvSource,
}

_source: Optional[DataSource] = None
_row_source: Optional[RowSource] = None


def _pick(registry: dict, env_var: str, default: str):
    kind = os.environ.get(env_var, default).strip().lower()
    try:
        return registry[kind]()
    except KeyError:
        raise ValueError(
            f"未知数据源 {env_var}={kind!r}，已注册：{sorted(registry)}"
        ) from None


def get_source() -> DataSource:
    """取游标源（给 db.py 的 connect 用）。"""
    global _source
    if _source is None:
        _source = _pick(_CURSOR_SOURCES, "FDE_DS", "pg")
    return _source


def get_row_source() -> RowSource:
    """取行源（给对账、补数用）。"""
    global _row_source
    if _row_source is None:
        _row_source = _pick(_ROW_SOURCES, "FDE_ROW_DS", "csv")
    return _row_source


def set_source(src) -> None:
    """注入指定游标源（测试或一次性验收用）。"""
    global _source
    _source = src


def set_row_source(src) -> None:
    """注入指定行源。"""
    global _row_source
    _row_source = src


def reset_source() -> None:
    """丢弃已选的源，下次 get_*_source() 重新按环境变量选。"""
    global _source, _row_source
    _source = None
    _row_source = None
