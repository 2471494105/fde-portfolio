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

    def test_content_changed(self):
        self.assertTrue(content_changed("h1", "h2"))
        self.assertFalse(content_changed("h1", "h1"))
        self.assertFalse(content_changed(None, "h2"))  # 上游没给指纹时不判「改了」


if __name__ == "__main__":
    unittest.main()
