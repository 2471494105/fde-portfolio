"""增量决策的边界测试。

这里不测数据库，只测「谁算新数据」这一个判断。
因为它错了会非常安静：数据照样入库，只是少了、多了、或者永远不更新。
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
        self.assertEqual(mark, datetime(2026, 11, 2, 8, 0, 0))

    def test_strictly_greater_than_mark(self):
        mark = datetime(2026, 11, 5, 0, 0, 0)
        rows = [
            _r(1, "2026-11-04 23:00:00"),
            _r(2, "2026-11-05 00:00:00"),
            _r(3, "2026-11-05 01:00:00"),
        ]
        good, bad, new_mark = plan(rows, mark, NOW)
        self.assertEqual([r["id"] for r in good], [3])
        self.assertEqual(new_mark, datetime(2026, 11, 5, 1, 0, 0))

    def test_future_rows_go_to_quarantine(self):
        mark = datetime(2026, 11, 4, 8, 0, 0)
        rows = [_r(1, "2026-11-05 08:00:00"), _r(2, "2027-01-01 08:00:00")]
        good, bad, new_mark = plan(rows, mark, NOW)
        self.assertEqual([r["id"] for r in good], [1])
        self.assertEqual([r["id"] for r in bad], [2])
        self.assertEqual(new_mark, datetime(2026, 11, 5, 8, 0, 0))

    def test_future_row_never_moves_watermark(self):
        mark = datetime(2026, 11, 4, 8, 0, 0)
        rows = [_r(1, "2027-01-01 08:00:00")]
        good, bad, new_mark = plan(rows, mark, NOW)
        self.assertEqual(good, [])
        self.assertEqual(len(bad), 1)
        self.assertEqual(new_mark, mark)

    def test_empty_batch_keeps_mark(self):
        mark = datetime(2026, 11, 5, 0, 0, 0)
        good, bad, new_mark = plan([], mark, NOW)
        self.assertEqual((good, bad, new_mark), ([], [], mark))

    def test_accepts_datetime_objects(self):
        mark = datetime(2026, 11, 1, 0, 0, 0)
        rows = [_r(1, datetime(2026, 11, 2, 0, 0, 0))]
        good, _, _ = plan(rows, mark, NOW)
        self.assertEqual(len(good), 1)


if __name__ == "__main__":
    unittest.main()
