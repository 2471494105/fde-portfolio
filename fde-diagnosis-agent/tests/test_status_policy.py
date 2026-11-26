"""口径登记的回归：换了客户，谁该拒、谁该跑。

这套测试守的不是「代码能跑」，是「口径只有一处定义」。
没有它，三处字面量哪天被谁手改一处，都没人知道——只让某处行为悄悄变。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import status_policy as sp  # noqa: E402

SCHEMA = ROOT / "data" / "schema.sql"


class StatusPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = sp.load()

    def test_client_a_closed_states_come_from_registry(self):
        """闭环词表 = 登记表里的那一版，不是代码里的某一版。"""
        self.assertEqual(sp.closed_states(policy=self.policy), ("已完成", "已合并", "已取消"))
        self.assertTrue(self.policy["clients"]["client_a"]["registered"])

    def test_unregistered_client_refuses_instead_of_guessing(self):
        """未登记客户：拒跑。返回空词表是最坏的结果——它会把「全部未闭环」当结论。"""
        with self.assertRaises(sp.PolicyNotRegistered):
            sp.closed_states("client_b_odoo", self.policy)
        with self.assertRaises(sp.PolicyNotRegistered):
            sp.open_predicate("status", "client_b_odoo", self.policy)
        with self.assertRaises(sp.PolicyNotRegistered):
            sp.sql_in_list(sp.closed_states("client_b_odoo", self.policy))

    def test_predicate_carries_words_as_parameter_not_as_text(self):
        """词必须是参数，不能拼进 SQL 字符串——否则又是一份手抄。"""
        where, params = sp.open_predicate("status", policy=self.policy)
        closed = sp.closed_states(policy=self.policy)
        for w in closed:
            self.assertNotIn(w, where)
        self.assertEqual(params, (list(closed),))

    def test_schema_index_matches_registry(self):
        """A 判据：索引谓词（唯一不能读 JSON 的落点）必须和登记表一致。"""
        states = sp.schema_predicate_states(SCHEMA.read_text(encoding="utf-8"))
        closed = tuple(sp.closed_states(policy=self.policy))
        self.assertEqual(set(states), set(closed), "索引谓词里的词和登记表不一致")
        self.assertEqual(sp.drift(states, closed), [])

    def test_drift_detects_a_single_word_lost(self):
        """少一个词就得报，不能「差不多就行」。"""
        closed = tuple(sp.closed_states(policy=self.policy))
        self.assertTrue(sp.drift(closed[:-1], closed))
        self.assertTrue(sp.drift(closed + ("新增状态",), closed))

    def test_unknown_state_is_named_not_silently_classified(self):
        """库里冒出未登记状态：必须被点名，否则会被机械地归进「未闭环」。"""
        c = sp.client("client_a", self.policy)
        self.assertEqual(sp.unknown_states(("待处理", "处理中", "已完成"), c), [])
        self.assertEqual(sp.unknown_states(("待处理", "待验收"), c), ["待验收"])

    def test_summary_is_serializable_and_carries_version(self):
        """报告里要能回答「这份绿按哪版口径判的」。"""
        s = sp.policy_summary(policy=self.policy)
        self.assertEqual(s["version"], self.policy["version"])
        self.assertEqual(s["client"], self.policy["default_client"])
        self.assertEqual(s["closed_states"], list(sp.closed_states(policy=self.policy)))
        self.assertGreaterEqual(len(s["clients"]), 2)


if __name__ == "__main__":
    unittest.main()
