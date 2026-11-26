"""删除同步的边界测试。

不测数据库，只测「谁该被标删」这一个判断。它错了会静默：删多了数据凭空消失，
删少了过期数据一直在回答。
"""

import sys
import unittest
from pathlib import Path

import app.ds.delete_sync as ds
from app.ds.delete_sync import content_changed, plan_deletes

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_ds_delete as cdd  # noqa: E402


def _acts(result):
    return {d["id"]: d["action"] for d in result["decisions"]}


class PlanDeletesTest(unittest.TestCase):
    def test_first_absence_only_marks(self):
        """一次缺席不动手：分页超时一次就会误删整批，第一次只记账。"""
        r = plan_deletes({"A", "B"}, {"B"})
        self.assertFalse(r["aborted"])
        self.assertEqual(_acts(r), {"A": "mark_absent"})
        self.assertEqual(r["decisions"][0]["absent_count"], 1)

    def test_second_absence_soft_deletes(self):
        r = plan_deletes({"A", "B"}, {"B"}, states={"A": {"absent_count": 1}})
        self.assertEqual(_acts(r), {"A": "soft_delete"})
        self.assertIn("连续缺席 2 次", r["decisions"][0]["reason"])

    def test_reappear_revives_and_resets(self):
        """回补或误判之后必须能撤销，否则第一次错就永久错。"""
        r = plan_deletes({"A", "B"}, {"A", "B"},
                         states={"A": {"absent_count": 2, "deleted": True}})
        self.assertEqual(_acts(r), {"A": "revive"})
        self.assertEqual(r["decisions"][0]["absent_count"], 0)

    def test_event_deletes_immediately(self):
        """明确 tombstone 是最强证据，不等第二次缺席。"""
        r = plan_deletes({"A", "B"}, {"B"}, events={"A": "upstream_purge"})
        self.assertEqual(_acts(r), {"A": "soft_delete"})
        self.assertIn("event:upstream_purge", r["decisions"][0]["reason"])

    def test_protected_never_deleted(self):
        """有未闭环工单引用：连缺 5 次也不删，转人工。"""
        r = plan_deletes({"A", "B"}, {"B"}, protected={"A"},
                         states={"A": {"absent_count": 5}})
        self.assertEqual(_acts(r), {"A": "protect"})
        self.assertEqual(r["decisions"][0]["absent_count"], 6)

    def test_snapshot_collapse_aborts_whole_round(self):
        """上游挂了返回空表：宁可这轮不判，也不能把全库软删。"""
        r = plan_deletes({"A", "B", "C", "D"}, {"A"})
        self.assertTrue(r["aborted"])
        self.assertEqual(r["decisions"], [])
        self.assertIn("接口没取全", r["reason"])

    def test_deleted_and_absent_is_noop(self):
        r = plan_deletes({"A", "B"}, {"B"},
                         states={"A": {"deleted": True, "absent_count": 3}})
        self.assertEqual(_acts(r), {"A": "noop"})

    def test_only_in_snapshot_is_not_a_delete_decision(self):
        """新出现的设备属于新增路径，不能在这里产生任何决策。"""
        r = plan_deletes({"B"}, {"A", "B"})
        self.assertEqual(r["decisions"], [])

    def test_stable_ids_produce_no_decisions(self):
        r = plan_deletes({"A", "B"}, {"A", "B"})
        self.assertEqual(r["decisions"], [])

    def test_event_cannot_override_protection(self):
        """删除事件是「最强证据」，但最强不等于能删掉有活工单的设备。

        这条是 11-26 补的：原实现把 event 分支排在 protected 前面，
        上游一句 purge 就能把有未闭环工单的设备删掉，工单随之悬空。
        """
        r = plan_deletes({"A", "B"}, {"B"}, protected={"A"},
                         events={"A": "upstream_purge"})
        self.assertEqual(_acts(r), {"A": "protect"})
        self.assertIn("未闭环引用", r["decisions"][0]["reason"])
        self.assertIn("upstream_purge", r["decisions"][0]["reason"])

    def test_event_does_not_delete_what_is_still_in_snapshot(self):
        """既在快照里、又收到删除事件：两个信号自相矛盾，选「不删」。

        删除不可逆，而矛盾下一次快照就可能自己消除；反过来删错了无法自动恢复。
        """
        r = plan_deletes({"A", "B"}, {"A", "B"}, events={"A": "upstream_purge"})
        self.assertEqual(_acts(r), {"A": "noop"})
        self.assertIn("矛盾", r["decisions"][0]["reason"])

    def test_protected_still_revives_when_it_reappears(self):
        """保护不阻止复活：受保护设备回到快照里，该清缺席计数（但保护标记本身不清）。"""
        r = plan_deletes({"A", "B"}, {"A", "B"}, protected={"A"},
                         states={"A": {"absent_count": 3}})
        self.assertEqual(_acts(r), {"A": "revive"})
        self.assertEqual(r["decisions"][0]["absent_count"], 0)

    def test_content_change_produces_resync(self):
        """在快照里、本地也有、指纹变了：该重灌这一条，而不是当它没变。"""
        r = plan_deletes({"A", "B"}, {"A", "B"},
                         states={"A": {"content_hash": "h1"}},
                         snapshot_hashes={"A": "h2", "B": "hx"})
        self.assertEqual(_acts(r), {"A": "resync"})
        self.assertEqual(r["decisions"][0]["content_hash"], "h2")

    def test_same_hash_no_resync(self):
        """指纹没变就不该重灌：否则每轮全量重写，增量退化成全量。"""
        r = plan_deletes({"A", "B"}, {"A", "B"},
                         states={"A": {"content_hash": "h1"}},
                         snapshot_hashes={"A": "h1"})
        self.assertEqual(r["decisions"], [])

    def test_no_hash_means_no_update_judgement(self):
        """不传指纹就完全不做更新判定，否则每个调用方都被迫先算 hash。"""
        r = plan_deletes({"A", "B"}, {"A", "B"}, states={"A": {"content_hash": "h1"}})
        self.assertEqual(r["decisions"], [])

    def test_first_time_hash_is_baseline_not_change(self):
        """本地没基线时只补基线，不判「改了」——否则首次同步会把每一行都重灌。"""
        r = plan_deletes({"A", "B"}, {"A", "B"}, states={"A": {}}, snapshot_hashes={"A": "h1"})
        self.assertEqual(r["decisions"], [])

    def test_revive_carries_new_hash(self):
        """复活时要把本轮指纹一并写回。

        不写回就会拿着旧指纹继续比，下一轮立刻又判一次 resync——重灌一次接一次。
        """
        r = plan_deletes({"A", "B"}, {"A", "B"},
                         states={"A": {"absent_count": 2, "deleted": True, "content_hash": "old"}},
                         snapshot_hashes={"A": "new"})
        self.assertEqual(_acts(r), {"A": "revive"})
        self.assertEqual(r["decisions"][0]["content_hash"], "new")

    def test_content_changed(self):
        self.assertTrue(content_changed("h1", "h2"))
        self.assertFalse(content_changed("h1", "h1"))
        self.assertFalse(content_changed(None, "h2"))  # 上游没给指纹时不判「改了」


class HashColumnsTest(unittest.TestCase):
    """指纹的列清单会随表结构漂移。漏一列不报错、不变红，只是那一列永远不改。"""

    def test_all_business_cols_covered(self):
        """表里 8 个业务列必须全部进指纹：漏一个就是那一列改了不重灌。"""
        cols = ["device_id", "line", "station", "camera_model", "lens_model",
                "light_model", "install_date", "maintenance_cycle_days",
                "_src", "_loaded_at"]
        r = cdd.hash_columns_check(cols)
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["excluded"], ["_src", "_loaded_at"])

    def test_new_business_col_is_reported(self):
        r = cdd.hash_columns_check(["device_id", "line", "model", "_src"])
        self.assertEqual(r["missing"], ["model"])

    def test_system_cols_do_not_trigger(self):
        """系统列天然会变，进了指纹每轮都判「变了」，等于退化成全量重灌。"""
        r = cdd.hash_columns_check(["device_id", "line", "station", "camera_model",
                                    "lens_model", "light_model", "install_date",
                                    "maintenance_cycle_days", "_loaded_at", "created_at"])
        self.assertEqual(r["missing"], [])

    def test_row_hash_ignores_system_cols(self):
        a = cdd.row_hash({"device_id": "A", "line": "1", "_src": "x"})
        b = cdd.row_hash({"device_id": "A", "line": "1", "_src": "y"})
        self.assertEqual(a, b)

    def test_resync_only_touches_given_cols(self):
        """快照没给的列绝不能用空值覆盖——那是误删本地数据。"""
        self.assertEqual(cdd.resync_cols({"device_id": "A", "line": "1"}), ["line"])
        self.assertEqual(cdd.resync_cols({"device_id": "A", "line": "1", "station": "宁波"}),
                         ["line", "station"])
        self.assertEqual(cdd.resync_cols({"device_id": "A"}), [])


class PolicyTest(unittest.TestCase):
    """报告里写出去的参数必须等于决策层真在用的默认值。

    手抄一份阈值出去，改常量时没人会记得改那行字；读报告的人就按旧规则判断。
    """

    def test_policy_is_derived_from_constants(self):
        p = cdd.policy()
        self.assertEqual(p["absent_confirm"], ds.ABSENT_CONFIRM)
        self.assertEqual(p["min_snapshot_ratio"], ds.MIN_SNAPSHOT_RATIO)
        self.assertIn("已完成", p["open_status_exclude"])

    def test_default_threshold_equals_policy_value(self):
        """缺席次数到阈值-1 不删、到阈值才删——用 policy 的值验，不写死 2。

        注意快照不能给空集：local 2 台、快照 0 台会先撞上骤降护栏（0 < 2×0.5），
        整轮 aborted、decisions 为空，缺席计数根本没机会跑。
        """
        t = cdd.policy()["absent_confirm"]
        self.assertEqual(
            _acts(plan_deletes({"A", "B"}, {"B"}, states={"A": {"absent_count": t - 2}})),
            {"A": "mark_absent"})
        self.assertEqual(
            _acts(plan_deletes({"A", "B"}, {"B"}, states={"A": {"absent_count": t - 1}})),
            {"A": "soft_delete"})


if __name__ == "__main__":
    unittest.main()
