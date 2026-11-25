"""行源抽象：只能按主键取一行的数据源。

与 DataSource（cursor 源）的区别不在实现方式，在**能力**：

- CursorSource（pg）：能跑任意 SQL，所以能给 cursor；
- RowSource（csv / excel / 只会 GET 的 HTTP 接口）：只能按主键取一条。

把后者硬塞进 connect() 的契约，会得到一个「有 connect 但不是游标」的怪东西——
工具一 `conn.cursor()` 就炸。所以分两级，各有各的协议。

这也是 capabilities() 存在的理由：选源时按能力挑，而不是按名字挑。
"""

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class RowSource(Protocol):
    name: str

    def keys(self, table: str) -> list:
        """这张表有哪些主键值。对账要遍历，就必须能枚举。"""
        ...

    def get_by_key(
        self, table: str, key: str, key_column: str = "device_id"
    ) -> Optional[dict]:
        """按主键取一行，取不到返回 None。

        刻意不抛异常：取不到是常态（客户台账漏了一台设备），
        和「源本身坏了」是两件事——后者交给 health()。
        两者混用一个 exception，上游就只能靠文案猜，而猜错会把
        「客户数据不全」记成「系统故障」。
        """
        ...

    def health(self) -> dict:
        """数据文件在不在、能不能解析。失败不抛，返回 {'ok': False, 'reason': ...}。"""
        ...

    def capabilities(self) -> list:
        """能提供哪些列 / 哪类操作。对账按它跳过给不了的字段，而不是硬比。"""
        ...
