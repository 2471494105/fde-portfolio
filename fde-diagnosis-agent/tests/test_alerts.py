"""中控台事件单测：不连库，全用手造两次运行。

这里钉的是「要不要叫醒人」这条规则，而不是页面长什么样：
  - 同一批红再跑一次不推（seen_count 涨、推送不涨）；
  - 红行新增/消失按编号比集合，条数相等不算没变化；
  - 变绿写「复跑归零」，不写「已修复」；
  - 老台账只有条数没有编号时，标 baseline，不假装「新增 0」。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import alerts as al  # noqa: E402

TZ = timezone(timedelta(hours=8))


def run(n, ids, status="fail"):
    return {"run_id": n, "ran_at": datetime(2026, 11, 26, 23, 0, tzinfo=TZ),
            "status": status, "red_count": len(ids) if ids is not None else n, "ids": ids}


class DiffIds(unittest.TestCase):
    def test_new_and_gone(self):
        d = al.diff_ids(["4", "5", "6"], ["4", "6", "8"])
        self.assertEqual(d["new"], ["8"])
        self.assertEqual(d["gone"], ["5"])
        self.assertEqual(d["same"], ["4", "6"])

    def test_count_equal_is_not_no_change(self):
        # 5 对 5 但换了一行：只比条数会判「没变化」，这是假绿
        d = al.diff_ids(["1", "2", "3", "4", "5"], ["1", "2", "3", "4", "9"])
        self.assertEqual(d["new"], ["9"])
        self.assertEqual(d["gone"], ["5"])


class Transition(unittest.TestCase):
    def test_first_red_from_green_notifies(self):
        out = al.plan_transition("defect_fields", run(1, []), run(2, ["4", "5"]))
        e = out["events"][0]
        self.assertTrue(e["notify"])
        self.assertEqual(e["dedup_key"], "defect_fields:red")
        self.assertEqual(e["detail"]["new_ids"], ["4", "5"])

    def test_same_rows_again_does_not_notify(self):
        out = al.plan_transition("defect_fields", run(1, ["4", "5"]), run(2, ["4", "5"]))
        e = out["events"][0]
        self.assertFalse(e["notify"], "同一批红再跑一次还推，就是刷屏")
        self.assertEqual(e["detail"]["same_count"], 2)

    def test_one_row_appears_notifies(self):
        out = al.plan_transition("defect_fields", run(1, ["4", "5"]), run(2, ["4", "5", "8"]))
        e = out["events"][0]
        self.assertTrue(e["notify"])
        self.assertEqual(e["detail"]["new_ids"], ["8"])
        self.assertIn("新增 1", e["title"])

    def test_one_row_fixed_no_notify_but_records_gone(self):
        out = al.plan_transition("defect_fields", run(1, ["4", "5"]), run(2, ["4"]))
        e = out["events"][0]
        self.assertFalse(e["notify"])
        self.assertEqual(e["detail"]["gone_ids"], ["5"])

    def test_back_to_zero_closes(self):
        out = al.plan_transition("defect_fields", run(1, ["4"]), run(2, []))
        self.assertEqual(out["events"], [])
        self.assertEqual(out["closes"][0]["dedup_key"], "defect_fields:red")
        self.assertIn("复跑归零", out["closes"][0]["reason"])
        self.assertNotIn("已修复", out["closes"][0]["reason"])

    def test_green_to_green_nothing(self):
        out = al.plan_transition("defect_fields", run(1, []), run(2, []))
        self.assertEqual(out["events"], [])
        self.assertFalse([c for c in out["closes"] if c["dedup_key"].endswith(":red")])

    def test_old_ledger_without_ids_is_baseline(self):
        # T22 之前只存条数：不能拿空集合去比，否则「新增 5」是编的
        prev = {"run_id": 1, "status": "fail", "red_count": 5, "ids": None}
        out = al.plan_transition("defect_fields", prev, run(2, ["4", "5"]))
        e = out["events"][0]
        self.assertTrue(e["detail"]["baseline"])
        self.assertTrue(e["notify"], "首次建立编号基线要推一次，之后才靠变化")
        self.assertEqual(e["detail"]["new_ids"], [])

    def test_owner_falls_back_to_unassigned(self):
        out = al.plan_transition("unknown_check", run(1, []), run(2, ["x"]))
        self.assertEqual(out["events"][0]["owner"], "待指派")

    def test_error_status_is_amber(self):
        cur = {"run_id": 2, "status": "error", "red_count": 0, "ids": [],
               "error": "连接超时", "ran_at": datetime(2026, 11, 26, tzinfo=TZ)}
        out = al.plan_transition("defect_fields", run(1, []), cur)
        self.assertEqual(out["events"][0]["severity"], "amber")
        self.assertIn("跑挂", out["events"][0]["title"])


class Silence(unittest.TestCase):
    now = datetime(2026, 11, 26, 23, 0, tzinfo=TZ)

    def test_recent_run_not_silent(self):
        last = {"defect_fields": {"ran_at": self.now - timedelta(hours=2)}}
        self.assertEqual(al.silent_events(last, self.now, expected=["defect_fields"]), [])

    def test_stale_run_is_amber(self):
        last = {"defect_fields": {"ran_at": self.now - timedelta(hours=30)}}
        ev = al.silent_events(last, self.now, expected=["defect_fields"])[0]
        self.assertEqual(ev["severity"], "amber")
        self.assertEqual(ev["dedup_key"], "defect_fields:silent")
        self.assertIn("30 小时", ev["title"])

    def test_never_run_counts_as_silent(self):
        ev = al.silent_events({}, self.now, expected=["defect_fields"])[0]
        self.assertIn("从没跑过", ev["title"])

    def test_bypass_never_run_checks_by_default(self):
        # 从没跑过的判据由调用方显式给 expected；不给就不编事件
        self.assertEqual(al.silent_events({}, self.now, expected=[]), [])


class Overdue(unittest.TestCase):
    today = date(2026, 12, 10)

    def test_past_due_open_is_red(self):
        ev = al.overdue_events(
            [{"rm_no": "RM-1", "state": "open", "due_on": date(2026, 12, 3), "violation_count": 362}],
            self.today,
        )[0]
        self.assertEqual(ev["severity"], "red")
        self.assertIn("逾期 7 天", ev["title"])
        self.assertEqual(ev["dedup_key"], "RM-1:overdue")

    def test_not_due_or_closed_is_skipped(self):
        rows = [
            {"rm_no": "RM-1", "state": "open", "due_on": date(2026, 12, 20)},
            {"rm_no": "RM-2", "state": "closed", "due_on": date(2026, 11, 1)},
        ]
        self.assertEqual(al.overdue_events(rows, self.today), [])


class Summarize(unittest.TestCase):
    def test_counts_exclude_closed(self):
        s = al.summarize([
            {"state": "open", "severity": "red"},
            {"state": "acked", "severity": "amber"},
            {"state": "closed", "severity": "red"},
        ])
        self.assertEqual(s, {"open": 2, "red": 1, "amber": 1, "acked": 1})


if __name__ == "__main__":
    unittest.main()
