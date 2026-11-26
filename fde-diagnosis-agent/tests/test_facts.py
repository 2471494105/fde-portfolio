"""事实层的规则测试：改一个值必须成对关开，没给的不许关，重复同步不许长行。

这里不连库，只钉住「什么算一次版本变更」。真正落库、撞唯一索引、沙箱重置那三步
由 `scripts/check_facts.py` 跑。
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ds.facts import (  # noqa: E402
    FACT_KEYS,
    fact_hash,
    fact_value,
    plan_facts,
    plan_issues,
    rows_to_facts,
)

DEVICE_ROW = {
    "device_id": "CX-101", "line": "3号线", "station": "1-3号工位",
    "camera_model": "In-Sight 2801", "lens_model": "16mm",
    "light_model": "CA-DC21E", "install_date": "2025-03-12",
    "maintenance_cycle_days": 90,
}


def current_of(incoming, ids=None):
    """把 incoming 变成「库里已经有这些当前值」的样子，id 按顺序发。"""
    ids = ids or {}
    out = {}
    for i, (k, v) in enumerate(sorted(incoming.items()), start=1):
        out[k] = {"id": ids.get(k, i), "value_hash": fact_hash(v)}
    return out


class ValueShapeTest(unittest.TestCase):
    def test_date_becomes_iso_string(self):
        self.assertEqual(fact_value(date(2025, 3, 12)), "2025-03-12")

    def test_plain_values_pass_through(self):
        for v in ("3号线", 90, None, True):
            with self.subTest(v=v):
                self.assertEqual(fact_value(v), v)

    def test_hash_is_stable_across_key_order(self):
        a = fact_hash({"x": 1, "y": 2})
        b = fact_hash({"y": 2, "x": 1})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_hash_differs_on_value_change(self):
        self.assertNotEqual(fact_hash("3号线"), fact_hash("4号线"))


class RowsToFactsTest(unittest.TestCase):
    def test_flattens_each_attribute(self):
        out = rows_to_facts([DEVICE_ROW])
        self.assertEqual(len(out), len(FACT_KEYS))
        self.assertEqual(out[("CX-101", "line")], "3号线")
        self.assertEqual(out[("CX-101", "maintenance_cycle_days")], 90)

    def test_skips_row_without_entity(self):
        self.assertEqual(rows_to_facts([{"line": "3号线"}]), {})
        self.assertEqual(rows_to_facts([{"device_id": "  ", "line": "3号线"}]), {})

    def test_missing_column_is_not_empty_value(self):
        """快照没给这一列，不等于这一列的值为空——它只是没被说出来。"""
        out = rows_to_facts([{"device_id": "CX-101", "line": "3号线"}])
        self.assertEqual(set(out), {("CX-101", "line")})

    def test_null_column_is_not_empty_value(self):
        """pg 落地表里「少给一列」就是 NULL。把 NULL 当成清空会一夜抹平历史。"""
        row = dict(DEVICE_ROW, station=None, install_date=None)
        out = rows_to_facts([row])
        self.assertNotIn(("CX-101", "station"), out)
        self.assertNotIn(("CX-101", "install_date"), out)
        self.assertIn(("CX-101", "line"), out)


class PlanFactsTest(unittest.TestCase):
    def test_first_round_opens_everything(self):
        incoming = rows_to_facts([DEVICE_ROW])
        plan = plan_facts({}, incoming)
        self.assertEqual(len(plan["opens"]), len(FACT_KEYS))
        self.assertEqual(plan["closes"], [])
        self.assertTrue(all(o["supersedes"] is None for o in plan["opens"]))

    def test_same_value_is_noop(self):
        incoming = rows_to_facts([DEVICE_ROW])
        plan = plan_facts(current_of(incoming), incoming)
        self.assertEqual((len(plan["opens"]), len(plan["closes"])), (0, 0))
        self.assertEqual(len(plan["noops"]), len(FACT_KEYS))

    def test_changed_value_closes_and_opens_the_same_key(self):
        incoming = rows_to_facts([DEVICE_ROW])
        cur = current_of(incoming)
        cur[("CX-101", "line")]["id"] = 777
        changed = dict(DEVICE_ROW, line="1号线B")
        plan = plan_facts(cur, rows_to_facts([changed]))
        self.assertEqual(len(plan["closes"]), 1)
        self.assertEqual(len(plan["opens"]), 1)
        self.assertEqual(plan["closes"][0]["id"], 777)
        self.assertEqual(plan["opens"][0]["supersedes"], 777)
        self.assertEqual(plan["opens"][0]["fact_key"], "line")

    def test_missing_key_is_uncovered_not_closed(self):
        """上游这次少给一列：不许关掉，否则一次分页不全就把历史抹平。"""
        full = rows_to_facts([DEVICE_ROW])
        partial = rows_to_facts([{"device_id": "CX-101", "line": "3号线"}])
        plan = plan_facts(current_of(full), partial)
        self.assertEqual(plan["closes"], [])
        self.assertEqual([u["fact_key"] for u in plan["uncovered"]],
                         sorted(k for k in FACT_KEYS if k != "line"))

    def test_station_change_does_not_touch_line(self):
        cur = current_of(rows_to_facts([DEVICE_ROW]))
        changed = dict(DEVICE_ROW, station="4-5号工位")
        plan = plan_facts(cur, rows_to_facts([changed]))
        self.assertEqual([o["fact_key"] for o in plan["opens"]], ["station"])
        self.assertEqual([c["fact_key"] for c in plan["closes"]], ["station"])

    def test_illegal_basis_is_refused(self):
        with self.assertRaises(ValueError):
            plan_facts({}, rows_to_facts([DEVICE_ROW]), basis="probably")

    def test_basis_is_carried_on_every_open(self):
        plan = plan_facts({}, rows_to_facts([DEVICE_ROW]), basis="effective")
        self.assertTrue(all(o["valid_from_basis"] == "effective" for o in plan["opens"]))


class PlanIssuesTest(unittest.TestCase):
    def test_good_plan_has_no_issues(self):
        incoming = rows_to_facts([DEVICE_ROW])
        plan = plan_facts({}, incoming)
        self.assertEqual(plan_issues(plan), [])

    def test_close_without_open_is_flagged(self):
        plan = {"closes": [{"id": 1, "entity_id": "CX-101", "fact_key": "line"}],
                "opens": [], "noops": [], "uncovered": []}
        self.assertTrue(any("没开新行" in x for x in plan_issues(plan)))

    def test_open_without_supersedes_is_flagged(self):
        plan = {"closes": [{"id": 1, "entity_id": "CX-101", "fact_key": "line"}],
                "opens": [{"entity_id": "CX-101", "fact_key": "line",
                           "value_hash": "h", "supersedes": None,
                           "valid_from_basis": "ingested"}],
                "noops": [], "uncovered": []}
        self.assertTrue(any("supersedes" in x for x in plan_issues(plan)))

    def test_close_without_id_is_flagged(self):
        plan = {"closes": [{"id": None, "entity_id": "CX-101", "fact_key": "line"}],
                "opens": [], "noops": [], "uncovered": []}
        self.assertTrue(any("没有 id" in x for x in plan_issues(plan)))

    def test_open_without_hash_is_flagged(self):
        plan = {"closes": [], "opens": [{"entity_id": "CX-101", "fact_key": "line",
                                         "value_hash": "", "supersedes": None,
                                         "valid_from_basis": "ingested"}],
                "noops": [], "uncovered": []}
        self.assertTrue(any("值指纹" in x for x in plan_issues(plan)))


class RetireTest(unittest.TestCase):
    """设备被判删之后，事实层必须「关而不开」。

    这一组是 L1 最容易被忘掉的一类：改值有测试、重复同步有测试、NULL 有测试，
    而「设备整个没了」没有——它不报错，只是让「当前设备清单」永远多一台。
    """

    def _current_two_devices(self):
        rows = [dict(DEVICE_ROW),
                dict(DEVICE_ROW, device_id="CX-102", line="4号线")]
        incoming = rows_to_facts(rows)
        return incoming, current_of(incoming)

    def test_deleted_entity_retires_every_current_key(self):
        incoming, current = self._current_two_devices()
        # 快照里只剩 CX-102，L-1 明确判删 CX-101
        left = {k: v for k, v in incoming.items() if k[0] == "CX-102"}
        plan = plan_facts(current, left, retire_entities=[
            {"entity_id": "CX-101", "reason": "upstream_purge"}])
        self.assertEqual(len(plan["retire"]), 7)
        self.assertEqual({r["fact_key"] for r in plan["retire"]}, set(FACT_KEYS))
        self.assertTrue(all(r["id"] is not None for r in plan["retire"]))
        self.assertTrue(all(r["reason"] == "upstream_purge" for r in plan["retire"]))
        # 关而不开：不能顺手给 CX-101 开新值
        self.assertFalse([o for o in plan["opens"] if o["entity_id"] == "CX-101"])

    def test_retire_only_touches_the_named_entity(self):
        incoming, current = self._current_two_devices()
        left = {k: v for k, v in incoming.items() if k[0] == "CX-102"}
        plan = plan_facts(current, left, retire_entities=[{"entity_id": "CX-101"}])
        self.assertEqual({r["entity_id"] for r in plan["retire"]}, {"CX-101"})
        # CX-101 同时出现在两个结果里，这是对的：uncovered 说的是「这轮没给」
        # （一个事实），retire 说的是「L-1 已经判删、该关」（一个决策）。
        # CX-102 在快照里、值没变 → noop，两处都不该出现。
        self.assertEqual({u["entity_id"] for u in plan["uncovered"]}, {"CX-101"})
        self.assertEqual({n["entity_id"] for n in plan["noops"]}, {"CX-102"})

    def test_entity_both_deleted_and_in_snapshot_is_a_conflict(self):
        """上游自相矛盾（标了删除又发回来）：不在这里替它决定，只露出来。"""
        incoming, current = self._current_two_devices()
        plan = plan_facts(current, incoming, retire_entities=[{"entity_id": "CX-101"}])
        self.assertEqual(plan["retire"], [])
        self.assertEqual(plan["retire_conflicts"], ["CX-101"])

    def test_retire_without_id_is_flagged(self):
        plan = {"closes": [], "opens": [], "noops": [], "uncovered": [],
                "retire": [{"id": None, "entity_id": "CX-101", "fact_key": "line"}]}
        self.assertTrue(any("没有 id" in x for x in plan_issues(plan)))

    def test_retire_and_open_on_same_key_is_flagged(self):
        plan = {"closes": [], "noops": [], "uncovered": [],
                "opens": [{"entity_id": "CX-101", "fact_key": "line",
                           "value_hash": "h", "supersedes": None,
                           "valid_from_basis": "ingested"}],
                "retire": [{"id": 1, "entity_id": "CX-101", "fact_key": "line"}]}
        self.assertTrue(any("两种结论冲突" in x for x in plan_issues(plan)))


if __name__ == "__main__":
    unittest.main()
