"""数据源接缝的契约测试。

不测「pg 能不能连库」——那要真库，慢且不稳。这里测三件更要紧的事：

1. 工具侧拿连接时确实经过 app.ds，而不是自己偷偷 psycopg.connect；
2. 换源只需 set_source 一行，调用方无感；
3. 未注册的源立刻报错，不静默回落到 pg。

第 1 条是核心：它保证「加壳」不是自欺——壳被真的走过了。
"""

import os
import unittest

from app.ds import get_source, reset_source, set_source


class FakeSource:
    """不连任何东西的假源。返回字符串，就是为了让真连接不可能混进来。"""

    name = "fake"

    def __init__(self):
        self.connects = 0

    def connect(self):
        self.connects += 1
        return f"fake-conn-{self.connects}"

    def health(self):
        return {"ok": True, "name": self.name}

    def capabilities(self):
        return ["fake"]


class TestDataSourceContract(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("FDE_DS", None)
        reset_source()

    def tearDown(self):
        os.environ.pop("FDE_DS", None)
        if self._env is not None:
            os.environ["FDE_DS"] = self._env
        reset_source()

    def test_default_is_pg(self):
        self.assertEqual(get_source().name, "pg")

    def test_db_connect_goes_through_source(self):
        fake = FakeSource()
        set_source(fake)
        from app.tools.db import connect

        self.assertEqual(connect(), "fake-conn-1")
        self.assertEqual(connect(), "fake-conn-2")
        self.assertEqual(fake.connects, 2)

    def test_unknown_source_raises(self):
        os.environ["FDE_DS"] = "mysql"
        reset_source()
        with self.assertRaises(ValueError):
            get_source()

    def test_reset_restores_default(self):
        set_source(FakeSource())
        reset_source()
        self.assertEqual(get_source().name, "pg")

    def test_capabilities_declared(self):
        caps = get_source().capabilities()
        self.assertIn("sql", caps)
        self.assertIn("read_only", caps)


if __name__ == "__main__":
    unittest.main()
