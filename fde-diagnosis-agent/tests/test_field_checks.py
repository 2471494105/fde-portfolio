"""缺陷字段判据（K）单测：不连库，全用手造行。

每条规则都要有正例与反例，理由和 DOC-60 一样：判据只能证明自己会红，
才配说别人是绿的。这里专门钉三处容易「看起来对」的写法：
  - 边界值（0 与 100 算合格，100.5 与 -0.5 算越界）——区间闭合与否是口径，不是手感；
  - 判定时刻注入（不给 now 就会拿本机钟，边界行随机红/绿）；
  - NULL 与空串分开（合并成一个「空」就把契约 R2 又抹平一次）。
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import field_checks as fc  # noqa: E402
from app.ds import status_policy as sp  # noqa: E402

TZ = timezone(timedelta(hours=8))


def spec() -> dict:
    return json.loads((ROOT / "data" / "mapping" / "field_rules.json").read_text(encoding="utf-8"))


def row(rec_id, conf=None, captured=None, lot="B1"):
    r = {"rec_id": rec_id, "conf_pct": conf, "lot_no": lot}
    if captured is not None:
        r["captured_at"] = captured
    return r


class ConfRangeTest(unittest.TestCase):
    def test_boundaries_are_inclusive(self):
        rows = [row(1, 0), row(2, 100), row(3, 99.9)]
        out = fc.run("conf_pct_range", rows, spec(), "2026-11-26 23:00:00")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["ids"], [])

    def test_below_and_above_are_red(self):
        rows = [row(5, 150), row(6, -10), row(7, -0.5)]
        out = fc.run("conf_pct_range", rows, spec(), "2026-11-26 23:00:00")
        self.assertEqual(out["status"], "fail")
        self.assertEqual(sorted(out["ids"]), ["5", "6", "7"])

    def test_null_conf_is_missing_not_out_of_range(self):
        # 空值不是越界：说成越界会让人去改值，其实该先查为什么没给
        out = fc.run("conf_pct_range", [row(9, None)], spec(), "2026-11-26 23:00:00")
        self.assertEqual(out["status"], "ok")


class FutureTimeTest(unittest.TestCase):
    def test_injected_now_decides(self):
        rows = [row(4, 90, datetime(2027, 1, 1, 8, 0, tzinfo=TZ)),
                row(11, 90, datetime(2026, 11, 2, 8, 0, tzinfo=TZ))]
        out = fc.run("captured_at_not_future", rows, spec(), "2026-11-26 23:00:00")
        self.assertEqual(out["ids"], ["4"])

    def test_moving_now_flips_the_sentence(self):
        # 同一个 2027 行，把判定时刻挪到 2027 之后就不该再红——
        # 「未来」是相对判定时刻的，不是绝对属性
        rows = [row(4, 90, datetime(2027, 1, 1, 8, 0, tzinfo=TZ))]
        out = fc.run("captured_at_not_future", rows, spec(), "2027-02-01 00:00:00")
        self.assertEqual(out["status"], "ok")

    def test_string_datetime_is_accepted(self):
        rows = [{"rec_id": "4", "captured_at": "2027-01-01 08:00:00"}]
        out = fc.run("captured_at_not_future", rows, spec(), "2026-11-26 23:00:00")
        self.assertEqual(out["ids"], ["4"])


class LotBlankTest(unittest.TestCase):
    def test_null_and_empty_are_separate(self):
        rows = [row(8, 80, lot=""), row(10, 87, lot=None), row(1, 90, lot="B1")]
        out = fc.run("lot_no_blank_split", rows, spec(), "2026-11-26 23:00:00")
        self.assertEqual(out["null_ids"], ["10"])
        self.assertEqual(out["empty_ids"], ["8"])
        self.assertEqual(out["status"], "fail")

    def test_missing_column_is_not_dirty_data(self):
        # 这一列根本没取出来，是取数问题；记成数据脏会让人去库里找一个不存在的空
        out = fc.run("lot_no_blank_split", [{"rec_id": "1"}], spec(), "2026-11-26 23:00:00")
        self.assertEqual(out["status"], "ok")


class EntryTest(unittest.TestCase):
    def test_unknown_rule_raises(self):
        with self.assertRaises(sp.UnknownCheck):
            fc.run("no_such_rule", [], spec(), "2026-11-26 23:00:00")

    def test_registered_matches_impl(self):
        self.assertEqual(fc.registered(), set(spec()["rules"]))

    def test_run_marks_ran(self):
        out = fc.run("conf_pct_range", [row(1, 90)], spec(), "2026-11-26 23:00:00")
        self.assertTrue(out["ran"])
        self.assertEqual(out["checked"], 1)


if __name__ == "__main__":
    unittest.main()
