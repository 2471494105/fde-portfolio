"""巡检层的判据测试：隔离区探针的几种红，以及脚本侧的判定。

这两个函数存在的理由一样：护栏写在代码里不算数，得有一次真实的输入把它触发过。
这里不连库，只钉住「什么算红」——真正造 +1 的那一步由 check_ds_incremental 第 [11] 步跑。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_ds_incremental as cdd  # noqa: E402
import data_health as dh  # noqa: E402


class QuarantineProbeGateTest(unittest.TestCase):
    """data_health 侧：只认三个真查库的数字，不认自评。"""

    def test_normal_evidence_passes(self):
        qp = {"before": 1, "after": 2, "restored": 1, "delta": 1, "ok": True}
        self.assertEqual(dh.quarantine_probe_issues(qp), [])

    def test_missing_evidence_is_red(self):
        """没有证据等于没验过——这正是「delta 恒 0」那种状态。"""
        for qp in ({}, {"before": None, "after": None, "restored": None}):
            with self.subTest(qp=qp):
                self.assertEqual(len(dh.quarantine_probe_issues(qp)), 1)

    def test_no_growth_is_red(self):
        qp = {"before": 1, "after": 1, "restored": 1, "ok": True}
        out = dh.quarantine_probe_issues(qp)
        self.assertEqual(len(out), 1)
        self.assertIn("没造出 +1", out[0])

    def test_not_restored_is_red(self):
        qp = {"before": 1, "after": 2, "restored": 2, "ok": True}
        out = dh.quarantine_probe_issues(qp)
        self.assertEqual(len(out), 1)
        self.assertIn("没回收干净", out[0])

    def test_self_reported_ok_is_ignored(self):
        """脚本说自己 ok，但数字不对——判据只认数字。"""
        qp = {"before": 1, "after": 1, "restored": 1, "ok": True}
        self.assertEqual(len(dh.quarantine_probe_issues(qp)), 1)

    def test_extra_growth_is_red_too(self):
        """一次涨 2 条也不对：探针只插一行，涨 2 说明还有别的行在动。"""
        qp = {"before": 1, "after": 3, "restored": 1, "ok": True}
        self.assertEqual(len(dh.quarantine_probe_issues(qp)), 1)


class ProbeVerdictTest(unittest.TestCase):
    """脚本侧：落 JSON 之前先自己判一次。"""

    def test_good(self):
        ok, note = cdd.quarantine_probe_verdict(1, 2, 1)
        self.assertTrue(ok)
        self.assertIn("1 → 2", note)

    def test_no_growth(self):
        ok, note = cdd.quarantine_probe_verdict(1, 1, 1)
        self.assertFalse(ok)
        self.assertIn("没让隔离区涨 1 条", note)

    def test_left_rows_behind(self):
        ok, note = cdd.quarantine_probe_verdict(1, 2, 2)
        self.assertFalse(ok)
        self.assertIn("回收后剩", note)

    def test_from_empty_quarantine(self):
        """隔离区本来是空的也能测：0 → 1 → 0。"""
        self.assertTrue(cdd.quarantine_probe_verdict(0, 1, 0)[0])

    def test_probe_row_is_future_dated(self):
        """探针必须是未来时间：只有未来行必然进隔离区，不依赖任何水位。"""
        self.assertGreater(cdd.QUARANTINE_PROBE["ts"], "2027-01-01")


class TimeAxisGateTest(unittest.TestCase):
    """data_health 侧：时间轴登记与探针的几种红。

    K-3 的教训是「三个钟不一致时增量静默丢」。这里钉住的是：不一致必须变成红，
    而且不能靠增量脚本自评的 ok——自评和数字要分开看。
    """

    def _good(self):
        return {
            "registry": {"business_time_field": "ts"},
            "plan_ts_key": "ts",
            "static_issues": [],
            "static_ok": True,
            "probe": {"rows_added": 0, "left_behind": False, "ok": True},
            "ok": True,
        }

    def test_missing_evidence_is_red(self):
        for ta in ({}, None):
            with self.subTest(ta=ta):
                self.assertEqual(len(dh.time_axis_issues(ta)), 1)

    def test_inconsistent_registry_is_red(self):
        ta = self._good()
        ta["static_issues"] = ["本地水位记 _loaded_at、业务时间字段是 ts"]
        out = dh.time_axis_issues(ta)
        self.assertEqual(len(out), 1)
        self.assertIn("自相矛盾", out[0])

    def test_probe_that_landed_is_red(self):
        """业务时间早于水位的行被灌进来 = 代码读错了钟。"""
        ta = self._good()
        ta["probe"] = {"rows_added": 1, "left_behind": False, "ok": True}
        out = dh.time_axis_issues(ta)
        self.assertEqual(len(out), 1)
        self.assertIn("被灌进 1 条", out[0])

    def test_probe_left_row_behind_is_red(self):
        ta = self._good()
        ta["probe"] = {"rows_added": 0, "left_behind": True, "ok": True}
        out = dh.time_axis_issues(ta)
        self.assertEqual(len(out), 1)
        self.assertIn("没清理干净", out[0])

    def test_registry_not_used_is_red(self):
        """登记说 ts、plan 实际读别的字段：登记没被用上，等于没登记。"""
        ta = self._good()
        ta["plan_ts_key"] = "_loaded_at"
        out = dh.time_axis_issues(ta)
        self.assertEqual(len(out), 1)
        self.assertIn("不一致", out[0])

    def test_self_reported_ok_is_ignored(self):
        """脚本说 ok，但探针数字不对——判据只认数字。"""
        ta = self._good()
        ta["probe"] = {"rows_added": 2, "left_behind": False, "ok": True}
        self.assertEqual(len(dh.time_axis_issues(ta)), 1)

    def test_probe_evidence_missing_is_red(self):
        ta = self._good()
        ta["probe"] = {}
        out = dh.time_axis_issues(ta)
        self.assertEqual(len(out), 1)
        self.assertIn("证据不完整", out[0])

    def test_all_green_passes(self):
        self.assertEqual(dh.time_axis_issues(self._good()), [])


if __name__ == "__main__":
    unittest.main()
