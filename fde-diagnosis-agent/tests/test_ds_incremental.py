"""增量决策的边界测试。

这里不测数据库，只测「谁算新数据」这一个判断。
因为它错了会非常安静：数据照样入库，只是少了、多了、或者永远不更新。

水位是 (时间戳, 主键) 复合的，所以断言里连主键一起比。
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.ds.incremental import plan

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_ds_incremental as cdd  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0, 0)


def _r(i, ts):
    return {"id": i, "ts": ts}


class IncrementalPlanTest(unittest.TestCase):
    def test_empty_mark_takes_everything_except_future(self):
        rows = [_r(1, "2026-11-01 08:00:00"), _r(2, "2026-11-02 08:00:00")]
        good, bad, mark = plan(rows, None, NOW)
        self.assertEqual([r["id"] for r in good], [1, 2])
        self.assertEqual(bad, [])
        self.assertEqual(mark, (datetime(2026, 11, 2, 8, 0, 0), 2))

    def test_strictly_greater_than_mark(self):
        mark = (datetime(2026, 11, 5, 0, 0, 0), 2)
        rows = [
            _r(1, "2026-11-04 23:00:00"),
            _r(2, "2026-11-05 00:00:00"),
            _r(3, "2026-11-05 01:00:00"),
        ]
        good, bad, new_mark = plan(rows, mark, NOW)
        self.assertEqual([r["id"] for r in good], [3])
        self.assertEqual(new_mark, (datetime(2026, 11, 5, 1, 0, 0), 3))

    def test_future_rows_go_to_quarantine(self):
        mark = (datetime(2026, 11, 4, 8, 0, 0), 1)
        rows = [_r(1, "2026-11-05 08:00:00"), _r(2, "2027-01-01 08:00:00")]
        good, bad, new_mark = plan(rows, mark, NOW)
        self.assertEqual([r["id"] for r in good], [1])
        self.assertEqual([r["id"] for r in bad], [2])
        self.assertEqual(new_mark, (datetime(2026, 11, 5, 8, 0, 0), 1))

    def test_future_row_never_moves_watermark(self):
        mark = (datetime(2026, 11, 4, 8, 0, 0), 1)
        rows = [_r(1, "2027-01-01 08:00:00")]
        good, bad, new_mark = plan(rows, mark, NOW)
        self.assertEqual(good, [])
        self.assertEqual(len(bad), 1)
        self.assertEqual(new_mark, mark)

    def test_empty_batch_keeps_mark(self):
        mark = (datetime(2026, 11, 5, 0, 0, 0), 7)
        good, bad, new_mark = plan([], mark, NOW)
        self.assertEqual((good, bad, new_mark), ([], [], mark))

    def test_accepts_datetime_objects(self):
        mark = datetime(2026, 11, 1, 0, 0, 0)
        rows = [_r(1, datetime(2026, 11, 2, 0, 0, 0))]
        good, _, _ = plan(rows, mark, NOW)
        self.assertEqual(len(good), 1)

    # ---------- 复合水位本身 ----------

    def test_same_ts_larger_pk_is_new(self):
        """同一时间戳、主键更大 = 新数据。这就是 K-2 修的东西。"""
        mark = (datetime(2026, 11, 6, 8, 0, 0), 9020)
        rows = [_r(9021, "2026-11-06 08:00:00"), _r(9022, "2026-11-06 08:00:00")]
        good, _, new_mark = plan(rows, mark, NOW)
        self.assertEqual([r["id"] for r in good], [9021, 9022])
        self.assertEqual(new_mark, (datetime(2026, 11, 6, 8, 0, 0), 9022))

    def test_same_ts_smaller_pk_is_not_new(self):
        """残留边界：主键更小的补录单会被判已灌过。钉住它，不让它变成「以为不会漏」。"""
        mark = (datetime(2026, 11, 6, 8, 0, 0), 9022)
        rows = [_r(9019, "2026-11-06 08:00:00")]
        good, _, new_mark = plan(rows, mark, NOW)
        self.assertEqual(good, [])
        self.assertEqual(new_mark, mark)

    def test_legacy_datetime_mark_takes_whole_same_second(self):
        """旧格式水位（只有时间戳）退化成「同一秒全算新」，靠落库幂等兜住，不会漏。"""
        mark = datetime(2026, 11, 6, 8, 0, 0)
        rows = [_r(1, "2026-11-06 08:00:00"), _r(2, "2026-11-06 08:00:00")]
        good, _, _ = plan(rows, mark, NOW)
        self.assertEqual([r["id"] for r in good], [1, 2])

    def test_numeric_pk_compares_as_number_not_string(self):
        """字符串比较会把 "9" 排到 "10" 之后；数字主键必须按数值比。"""
        mark = (datetime(2026, 11, 1, 0, 0, 0), 9)
        rows = [_r(10, "2026-11-01 00:00:00")]
        good, _, new_mark = plan(rows, mark, NOW)
        self.assertEqual([r["id"] for r in good], [10])
        self.assertEqual(new_mark, (datetime(2026, 11, 1, 0, 0, 0), 10))

    def test_missing_pk_column_does_not_crash(self):
        """源表没有主键列时不该抛异常，退化成只比时间戳。"""
        mark = datetime(2026, 11, 1, 0, 0, 0)
        rows = [{"ts": "2026-11-02 00:00:00"}]
        good, _, new_mark = plan(rows, mark, NOW, pk_key=None)
        self.assertEqual(len(good), 1)
        self.assertEqual(new_mark, (datetime(2026, 11, 2, 0, 0, 0), None))

    # ---------- 重叠窗口：把上面那条残留边界补掉 ----------

    def test_overlap_picks_up_smaller_pk_late_arrival(self):
        """带 overlap 时，主键更小的补录单会被捞回来——这是补边界的正题。"""
        mark = (datetime(2026, 11, 6, 8, 0, 0), 9022)
        rows = [_r(9019, "2026-11-06 08:00:00")]
        good, _, _ = plan(rows, mark, NOW, overlap=timedelta(days=1))
        self.assertEqual([r["id"] for r in good], [9019])

    def test_overlap_never_moves_watermark_backwards(self):
        """重叠区全是旧行时，max 比原水位更早；水位必须原地不动，而不是倒退。

        若这里退化成「无条件取 max」，水位会从 11-06 退回 11-02，下一轮整段重捞。
        """
        mark = (datetime(2026, 11, 6, 8, 0, 0), 9022)
        rows = [_r(1, "2026-11-01 08:00:00"), _r(2, "2026-11-02 08:00:00")]
        good, _, new_mark = plan(rows, mark, NOW, overlap=timedelta(days=30))
        self.assertEqual([r["id"] for r in good], [1, 2])  # 重叠区确实被看了一遍
        self.assertEqual(new_mark, mark)                    # 但水位没动

    def test_overlap_still_quarantines_future_rows(self):
        """开窗口不等于放行未来时间：2027 那条仍然只进隔离区、不参与水位。"""
        mark = (datetime(2026, 11, 6, 8, 0, 0), 9022)
        rows = [_r(4, "2027-01-01 08:00:00")]
        good, bad, new_mark = plan(rows, mark, NOW, overlap=timedelta(days=30))
        self.assertEqual(good, [])
        self.assertEqual([r["id"] for r in bad], [4])
        self.assertEqual(new_mark, mark)

    def test_zero_overlap_behaves_like_none(self):
        """overlap=0 是显式关掉，不能把「不写」和「写 0」解释成两件事。"""
        mark = (datetime(2026, 11, 6, 8, 0, 0), 9022)
        rows = [_r(9019, "2026-11-06 08:00:00")]
        self.assertEqual(plan(rows, mark, NOW, overlap=timedelta(0))[0], [])
        self.assertEqual(plan(rows, mark, NOW)[0], [])


class OverlapPolicyTest(unittest.TestCase):
    """报告里写出去的窗口必须等于代码真在用的那个。"""

    def test_policy_matches_module_constant(self):
        p = cdd.overlap_policy()
        self.assertEqual(p["window_seconds"], int(cdd.OVERLAP_WINDOW.total_seconds()))
        self.assertGreater(p["window_seconds"], 0)

    def test_window_actually_recovers_the_leaked_row(self):
        """窗口开着就必须真的多捞一条：这是判据的实证，不是声明。

        不带窗口时主键更小的补录单会被判「已灌过」；带上窗口（只按时间比、
        不比主键）它才回到候选里。
        """
        mark = (datetime(2026, 11, 6, 8, 0, 0), 9022)
        rows = [_r(9019, "2026-11-06 08:00:00")]
        self.assertEqual(plan(rows, mark, NOW)[0], [])            # 漏
        self.assertEqual([r["id"] for r in plan(rows, mark, NOW, overlap=cdd.OVERLAP_WINDOW)[0]],
                         [9019])                                   # 补


if __name__ == "__main__":
    unittest.main()
