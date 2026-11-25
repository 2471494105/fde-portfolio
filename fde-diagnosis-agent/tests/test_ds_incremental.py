"""增量决策的边界测试。

这里不测数据库，只测「谁算新数据」这一个判断。
因为它错了会非常安静：数据照样入库，只是少了、多了、或者永远不更新。

水位是 (时间戳, 主键) 复合的，所以断言里连主键一起比。
"""

import unittest
from datetime import datetime

from app.ds.incremental import plan

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


if __name__ == "__main__":
    unittest.main()
