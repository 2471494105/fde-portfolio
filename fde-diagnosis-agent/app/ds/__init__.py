"""数据源注册与选择。

默认只有 pg。以后加形态（CSV / 外部 SQL）时，在这里注册一行；
具体接哪个由环境变量 FDE_DS 决定，代码里不出现 if/else 分支。

为什么要有 set_source / reset_source：
测试里要拿一个假源验证「工具确实走的是接口、而不是偷偷连了库」。
没有这两个钩子，就只能靠连真库来测，慢且不可控。
"""

import os
from typing import Optional

from app.ds.base import DataSource
from app.ds.pg import PgSource

_SOURCES = {
    "pg": PgSource,
}

_source: Optional[DataSource] = None


def get_source() -> DataSource:
    global _source
    if _source is None:
        kind = os.environ.get("FDE_DS", "pg").strip().lower()
        try:
            _source = _SOURCES[kind]()
        except KeyError:
            raise ValueError(
                f"未知数据源 FDE_DS={kind!r}，已注册：{sorted(_SOURCES)}"
            ) from None
    return _source


def set_source(src) -> None:
    """注入指定源（测试或一次性验收用）。"""
    global _source
    _source = src


def reset_source() -> None:
    """丢弃已选的源，下次 get_source() 重新按 FDE_DS 选。"""
    global _source
    _source = None
