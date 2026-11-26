"""删除同步的边界测试。

不测数据库，只测「谁该被标删」这一个判断。它错了会静默：删多了数据凭空消失，
删少了过期数据一直在回答。
"""

import unittest

from app.ds.delete_sync import content_changed, plan_deletes


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


if __name__ == "__main__":
    unittest.main()
