"""工位契约（判据 I/J）单测：不连库，全用手造行。

重点是「不猜」这件事能被反例证明：
  - 只有工位号、没有产线 → 红（四条产线上都成立，谁也不能替客户选一条）
  - 有产线、文本里没有工位号（「4号线」）→ 红
  - (line_no, seq) 查不到 / 查到多个 → 红
  - station_id 给了但字典里没有 → 红
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import station_contract as sc  # noqa: E402


def master():
    return [
        {"station_id": "S-A01", "station_no": "产线A-01", "line_no": "产线A", "seq": 1},
        {"station_id": "S-A02", "station_no": "产线A-02", "line_no": "产线A", "seq": 2},
        {"station_id": "S-B01", "station_no": "产线B-01", "line_no": "产线B", "seq": 1},
    ]


def registry(**over):
    reg = {
        "version": "test",
        "station_key": {
            "master_table": "md_station",
            "id_column": "station_id",
            "id_pattern": "^S-[A-D][0-9]{2}$",
            "business_code_column": "station_no",
            "line_column": "line_no",
            "seq_column": "seq",
            "expected_rows": 3,
        },
        "contracts": [
            {
                "id": "wo",
                "table": "biz_work_order",
                "key_column": "wo_no",
                "field": "station_id",
                "alternatives": ["prod_line", "station_no"],
                "owner": "产线",
                "due": "2026-12-03",
                "parse_rule": "x",
            }
        ],
        "field_gaps": [
            {"id": "g1", "contract": "wo", "missing": "缺产线", "owner": "产线", "due": "2026-12-03"}
        ],
    }
    reg.update(over)
    return reg


TODAY = date(2026, 11, 26)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.maps = sc.station_maps(master())
        self.c = registry()["contracts"][0]

    def test_station_id_hit(self):
        self.assertEqual(sc.resolve({"station_id": "S-A01"}, self.c, self.maps), ("S-A01", ""))

    def test_station_id_not_in_dict(self):
        sid, why = sc.resolve({"station_id": "S-Z99"}, self.c, self.maps)
        self.assertIsNone(sid)
        self.assertIn("不在", why)

    def test_station_number_without_line_is_red_not_guessed(self):
        """「1号工位」在 A/B 两条线上都成立——不能挑一条。"""
        sid, why = sc.resolve({"station_no": "1号工位"}, self.c, self.maps)
        self.assertIsNone(sid)
        self.assertIn("没有产线", why)

    def test_line_plus_number_resolves(self):
        self.assertEqual(sc.resolve({"prod_line": "产线B", "station_no": "1号工位"}, self.c, self.maps), ("S-B01", ""))

    def test_line_only_text_is_literal_not_station(self):
        sid, why = sc.resolve({"prod_line": "产线A", "station_no": "4号线"}, self.c, self.maps)
        self.assertIsNone(sid)
        self.assertIn("不是工位写法", why)

    def test_line_without_text(self):
        sid, why = sc.resolve({"prod_line": "产线A"}, self.c, self.maps)
        self.assertIsNone(sid)
        self.assertIn("没有工位文本", why)

    def test_pair_not_in_dict(self):
        sid, why = sc.resolve({"prod_line": "产线A", "station_no": "9号工位"}, self.c, self.maps)
        self.assertIsNone(sid)
        self.assertIn("不存在", why)

    def test_ambiguous_pair(self):
        dup = master() + [{"station_id": "S-A01X", "station_no": "产线A-01b", "line_no": "产线A", "seq": 1}]
        maps = sc.station_maps(dup)
        sid, why = sc.resolve({"prod_line": "产线A", "station_no": "1号工位"}, self.c, maps)
        self.assertIsNone(sid)
        self.assertIn("匹配到 2 个", why)

    def test_both_empty(self):
        sid, why = sc.resolve({}, self.c, self.maps)
        self.assertIsNone(sid)
        self.assertIn("既没有", why)


class StationKeyTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(sc.check_station_key(registry(), master()), [])

    def test_missing_column(self):
        rows = [{"station_no": "产线A-01", "line_no": "产线A", "seq": 1}]
        self.assertIn("没有 station_id 列", " ".join(sc.check_station_key(registry(), rows)))

    def test_blank_id(self):
        rows = master()
        rows[0]["station_id"] = ""
        self.assertIn("station_id 为空", " ".join(sc.check_station_key(registry(), rows)))

    def test_bad_pattern(self):
        rows = master()
        rows[0]["station_id"] = "A01"
        self.assertIn("不符合", " ".join(sc.check_station_key(registry(), rows)))

    def test_duplicate_id(self):
        rows = master()
        rows[1]["station_id"] = "S-A01"
        self.assertIn("重复", " ".join(sc.check_station_key(registry(), rows)))

    def test_row_count_drift(self):
        reg = registry()
        reg["station_key"]["expected_rows"] = 4
        self.assertIn("行数 3 != 登记的 4", " ".join(sc.check_station_key(reg, master())))

    def test_empty_master(self):
        self.assertIn("一行都没有", " ".join(sc.check_station_key(registry(), [])))


class AuditTest(unittest.TestCase):
    def test_counts_and_samples(self):
        rows = [
            {"wo_no": "WO-1", "station_id": None, "prod_line": None, "station_no": "1号工位"},
            {"wo_no": "WO-2", "station_id": "S-A01", "prod_line": None, "station_no": "1号工位"},
            {"wo_no": "WO-3", "station_id": None, "prod_line": "产线B", "station_no": "1号工位"},
        ]
        rep = sc.audit(registry(), {"wo": rows}, master(), today=TODAY)
        self.assertEqual(rep["contracts"][0]["violations"], 1)
        self.assertEqual(rep["contracts"][0]["samples"], ["WO-1"])
        self.assertEqual(rep["gaps"][0]["state"], "warn")

    def test_gap_closes_when_violations_zero(self):
        rows = [{"wo_no": "WO-2", "station_id": "S-A01", "prod_line": None, "station_no": "1号工位"}]
        rep = sc.audit(registry(), {"wo": rows}, master(), today=TODAY)
        self.assertEqual(rep["gaps"][0]["state"], "closed")
        self.assertEqual(rep["problems"], [])

    def test_gap_without_owner_is_red(self):
        reg = registry()
        reg["field_gaps"][0]["owner"] = ""
        rows = [{"wo_no": "WO-1", "station_id": None, "prod_line": None, "station_no": "1号工位"}]
        rep = sc.audit(reg, {"wo": rows}, master(), today=TODAY)
        self.assertEqual(rep["gaps"][0]["state"], "red")
        self.assertTrue(any("责任方" in p for p in rep["problems"]))

    def test_claimed_gap_turns_red_after_due(self):
        rows = [{"wo_no": "WO-1", "station_id": None, "prod_line": None, "station_no": "1号工位"}]
        rep = sc.audit(registry(), {"wo": rows}, master(), today=date(2026, 12, 10))
        self.assertEqual(rep["gaps"][0]["state"], "red")
        self.assertTrue(any("过期" in p for p in rep["problems"]))

    def test_gap_to_unknown_contract_raises(self):
        reg = registry()
        reg["field_gaps"][0]["contract"] = "nope"
        with self.assertRaises(sc.ContractNotRegistered):
            sc.audit(reg, {"wo": []}, master(), today=TODAY)

    def test_contract_problems_collected(self):
        rows = [{"wo_no": "WO-1", "station_id": None, "prod_line": None, "station_no": "1号工位"}]
        rep = sc.audit(registry(), {"wo": rows}, master(), today=TODAY)
        self.assertTrue(any("违约 1/1" in p for p in rep["problems"]))


class RegistryFileTest(unittest.TestCase):
    """真登记表自洽：字段齐、缺口指向的契约在、到期日是日期。"""

    def test_real_registry(self):
        reg = sc.load()
        self.assertEqual(len(reg["contracts"]), 2)
        for c in reg["contracts"]:
            for f in ("id", "table", "key_column", "field", "owner", "due"):
                self.assertTrue(c.get(f), f"{c['id']} 缺 {f}")
            date.fromisoformat(c["due"])
        ids = {c["id"] for c in reg["contracts"]}
        for g in reg["field_gaps"]:
            self.assertIn(g["contract"], ids)
        self.assertEqual(json.loads(json.dumps(reg)), reg)


if __name__ == "__main__":
    unittest.main()
