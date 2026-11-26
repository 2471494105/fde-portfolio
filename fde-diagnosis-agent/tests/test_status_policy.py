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
        """未登记客户：拒跑。返回空词表是最坏的结果——它会把「全部未闭环」当结论。

        反例必须用**真的**没登记的那家。原先这里拿 client_b_odoo 当反例，
        K-5 把它登记之后这条就失效了——用一家已登记的客户测「拒跑」，
        测到的是「拒跑机制还在」，不是「未登记会被拒」。
        """
        with self.assertRaises(sp.PolicyNotRegistered):
            sp.closed_states("client_c_unregistered", self.policy)
        with self.assertRaises(sp.PolicyNotRegistered):
            sp.open_predicate("status", "client_c_unregistered", self.policy)
        with self.assertRaises(sp.PolicyNotRegistered):
            sp.sql_in_list(sp.closed_states("client_c_unregistered", self.policy))

    def test_empty_vocab_is_reported_as_empty_not_as_unregistered(self):
        """登记了但词表为空，和压根没登记，是两种病：报错混在一起，修的人会找错地方。"""
        patched = {
            **self.policy,
            "clients": {
                **self.policy["clients"],
                "client_b_odoo": {
                    **self.policy["clients"]["client_b_odoo"], "closed_states": []},
            },
        }
        with self.assertRaises(sp.PolicyNotRegistered) as ctx:
            sp.closed_states("client_b_odoo", patched)
        self.assertIn("词表为空", str(ctx.exception))
        self.assertNotIn("未登记闭环口径", str(ctx.exception))

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


class FoldTest(unittest.TestCase):
    """K-5：折法是承重的——两家客户词不同，事实必须是同一件事。

    这四条守的是「登记了词表，就得说清每个词折成我方哪一态」。
    缺了这层，词表自己很自洽，保护集却静默失真。
    """

    @classmethod
    def setUpClass(cls):
        cls.policy = sp.load()

    def test_b_odoo_closed_states_come_from_registry(self):
        """Odoo 的闭环是 done + cancel；不是我们家的「已完成/已取消」字面。"""
        self.assertEqual(sp.closed_states("client_b_odoo", self.policy), ("done", "cancel"))

    def test_fold_is_complete_for_both_clients(self):
        """两家登记客户都必须过 D 判据；`client_c_unregistered` 不参与（它本来就该拒跑）。"""
        self.assertEqual(sp.fold_problems("client_a", self.policy), [])
        self.assertEqual(sp.fold_problems("client_b_odoo", self.policy), [])

    def test_fold_target_lands_in_canonical_vocabulary(self):
        """折法目标必须是我方基准词；折到基准外（笔误）下游永远匹配不上。"""
        fold = sp.vocab_fold("client_b_odoo", self.policy)
        canonical = set(self.policy["canonical_states"])
        self.assertEqual(set(fold) >= {"done", "cancel", "draft", "confirmed", "merged"}, True)
        self.assertEqual(set(fold.values()) <= canonical, True)
        self.assertEqual(fold["merged"], "已完成", "Odoo 无「已合并」，合并归档表现为 done")

    def test_word_without_fold_target_is_reported(self):
        """少一条折法：必须报，而不是落进「某一类」就算完。"""
        patched = {
            **self.policy,
            "clients": {
                **self.policy["clients"],
                "client_b_odoo": {
                    **self.policy["clients"]["client_b_odoo"],
                    "vocab_fold": {"cancel": "已取消"},
                },
            },
        }
        problems = sp.fold_problems("client_b_odoo", patched)
        self.assertTrue(any("done 没有折法目标" in p for p in problems), problems)

    def test_fold_target_outside_canonical_is_reported(self):
        """折到基准词表外：报，并说明多半是笔误。"""
        patched = {
            **self.policy,
            "clients": {
                **self.policy["clients"],
                "client_b_odoo": {
                    **self.policy["clients"]["client_b_odoo"],
                    "vocab_fold": {**sp.vocab_fold("client_b_odoo", self.policy),
                                   "done": "closed"},
                },
            },
        }
        problems = sp.fold_problems("client_b_odoo", patched)
        self.assertTrue(any("closed" in p and "不在我方基准词表" in p for p in problems), problems)

    def test_canonical_state_without_landing_must_be_declared_as_gap(self):
        """我方基准里有、这家客户没有的态，必须在 fold_gaps_states 认领——不认领就是假装它存在。"""
        base = self.policy["clients"]["client_b_odoo"]
        fold = {k: v for k, v in base["vocab_fold"].items() if k != "merged"}
        undeclared = {
            **self.policy,
            "clients": {**self.policy["clients"],
                        "client_b_odoo": {**base, "vocab_fold": fold, "fold_gaps_states": []}},
        }
        problems = sp.fold_problems("client_b_odoo", undeclared)
        self.assertTrue(any("已合并" in p and "没有落点" in p for p in problems), problems)

        # 认领了就该过；认领错了对象（基准里没有的词）也要报
        self.assertEqual(sp.fold_problems("client_b_odoo", {
            **self.policy,
            "clients": {**self.policy["clients"],
                        "client_b_odoo": {**base, "vocab_fold": fold,
                                          "fold_gaps_states": ["已合并"]}},
        }), [])
        self.assertTrue(sp.fold_problems("client_b_odoo", {
            **self.policy,
            "clients": {**self.policy["clients"],
                        "client_b_odoo": {**base, "vocab_fold": fold,
                                          "fold_gaps_states": ["已关闭"]}},
        }))


if __name__ == "__main__":
    unittest.main()
