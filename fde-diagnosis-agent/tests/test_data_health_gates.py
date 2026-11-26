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


if __name__ == "__main__":
    unittest.main()
