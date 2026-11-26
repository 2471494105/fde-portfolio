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


class UnsupportedCheckTest(unittest.TestCase):
    """T14：声明「不适用」必须能被机器证明真的没跑。

    这组测试守的不是「声明存在」，是「声明与执行一致」。静态对账只能证明字对上了；
    能不能证明「没跑」，靠 `run_check` 返回的 `ran`。
    """

    CHECK = "work_order_state_stage_conflict"
    COVER = "status_vocabulary_coverage"

    @classmethod
    def setUpClass(cls):
        cls.policy = sp.load()
        from app.ds import checks as ck
        cls.ck = ck

    def test_registry_has_both_checks_and_each_carries_a_place(self):
        """每条判据都要说清做什么、读什么、在哪跑——否则「登记」只是起个名字。"""
        reg = sp.checks_registry(self.policy)
        self.assertIn(self.CHECK, reg)
        self.assertIn(self.COVER, reg)
        for cid, spec in reg.items():
            for k in ("what", "reads", "enforced_where"):
                self.assertTrue(spec.get(k), f"{cid} 缺 {k}")
        self.assertEqual(sp.check_problems(self.policy), [])

    def test_declaration_is_id_keyed_not_prose(self):
        """id 必须能机器匹配；v2 那种 id+散文塞一个字符串的写法要显式拒绝，不做静默兼容。"""
        d = sp.unsupported_checks("client_b_odoo", self.policy)
        self.assertEqual(list(d), [self.CHECK])
        self.assertTrue(d[self.CHECK].strip())
        patched = {
            **self.policy,
            "clients": {**self.policy["clients"],
                        "client_b_odoo": {**self.policy["clients"]["client_b_odoo"],
                                          "unsupported_checks": [self.CHECK + "（理由）"]}},
        }
        with self.assertRaises(sp.PolicyNotRegistered):
            sp.unsupported_checks("client_b_odoo", patched)
        self.assertTrue(any("v2 字符串数组" in p for p in sp.check_problems(patched)))

    def test_check_applies_and_unknown_id_raises(self):
        self.assertFalse(sp.check_applies(self.CHECK, "client_b_odoo", self.policy))
        self.assertTrue(sp.check_applies(self.CHECK, "client_a", self.policy))
        self.assertTrue(sp.check_applies(self.COVER, "client_b_odoo", self.policy))
        with self.assertRaises(sp.UnknownCheck):
            sp.check_applies("stage_conflict_v1", "client_b_odoo", self.policy)

    def test_not_applicable_never_touches_rows(self):
        """声明不适用的客户，入口要返回 ran=False + checked=0，而且不读数据（rows=None 也不崩）。"""
        r = self.ck.run_check(self.CHECK, rows=None, name="client_b_odoo", policy=self.policy)
        self.assertEqual(r["status"], "not_applicable")
        self.assertFalse(r["ran"])
        self.assertEqual(r["checked"], 0)
        self.assertEqual(r["problems"], [])
        self.assertTrue(r["reason"])

    def test_applicable_client_really_runs_and_folds_words(self):
        """适用时真跑：A 词与 B 词都要能被折法对上，判出同一件事。"""
        rows = [
            {"wo_no": "W1", "state": "已完成", "stage_id": "进行中"},
            {"wo_no": "W2", "state": "已完成", "stage_id": "已关闭"},
            {"wo_no": "W3", "state": "处理中", "stage_id": "进行中"},
        ]
        a = self.ck.run_check(self.CHECK, rows=rows, name="client_a", policy=self.policy)
        self.assertTrue(a["ran"])
        self.assertEqual(a["checked"], 3)
        self.assertEqual(a["status"], "fail")
        self.assertEqual([p.split("：")[0] for p in a["problems"]], ["W1"])

        # 同一批事实换成 Odoo 词：done 折成「已完成」，红的是同一张单
        b_rows = [{"wo_no": "W1", "state": "done", "stage_id": "进行中"},
                  {"wo_no": "W2", "state": "done", "stage_id": "已关闭"}]
        stripped = {
            **self.policy,
            "clients": {**self.policy["clients"],
                        "client_b_odoo": {**self.policy["clients"]["client_b_odoo"],
                                          "unsupported_checks": {}}},
        }
        b = self.ck.run_check(self.CHECK, rows=b_rows, name="client_b_odoo", policy=stripped)
        self.assertEqual(b["status"], "fail")
        self.assertIn("done→已完成", b["problems"][0])

    def test_coverage_check_flags_unknown_word_on_any_client(self):
        """这条判据对所有客户都适用：它只问「这个词我认识吗」。它是 E 判据的对照组。"""
        rows = [{"wo_no": "W1", "state": "rework"},
                {"wo_no": "W2", "state": "draft"},        # B 的已知未闭环
                {"wo_no": "W3", "state": "done"}]         # B 的闭环
        r = self.ck.run_check(self.COVER, rows=rows, name="client_b_odoo", policy=self.policy)
        self.assertTrue(r["ran"])
        self.assertEqual(r["status"], "fail")
        self.assertEqual(len(r["problems"]), 1)
        self.assertIn("rework", r["problems"][0])

    def test_every_client_must_keep_at_least_one_check_running(self):
        """一家客户可以把某条判据声明不适用，但不该关到一条都不跑——那等于没人管。"""
        all_off = {
            **self.policy,
            "clients": {**self.policy["clients"],
                        "client_b_odoo": {**self.policy["clients"]["client_b_odoo"],
                                          "unsupported_checks": {
                                              self.CHECK: "不适用", self.COVER: "也不适用"}}},
        }
        self.assertTrue(any("把所有登记判据都声明不适用" in p
                            for p in sp.check_problems(all_off)))

    def test_registered_but_unimplemented_check_is_reported(self):
        """登记了没实现，等于永远不会判出东西——比「被关掉」更安静。"""
        patched = {
            **self.policy,
            "checks": {**self.policy["checks"],
                       "ghost_check": {"what": "只有名字", "reads": ["state"],
                                       "enforced_where": "不存在"}},
        }
        self.assertTrue(any("ghost_check" in p and "没有实现" in p
                            for p in sp.check_problems(patched)))
        with self.assertRaises(sp.UnknownCheck):
            self.ck.run_check("ghost_check", rows=[], policy=patched)


if __name__ == "__main__":
    unittest.main()
