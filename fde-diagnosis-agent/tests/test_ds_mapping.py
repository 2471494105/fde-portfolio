"""字段映射的契约测试。

测的是「翻译」的边界：该翻的翻对、该放的放行、该报的报清楚。
不测 JSON 能不能读——那太基础。

两个边界要分清，混了就会出事：
  缺列  = 配置过时或文件变了 → 必须报
  空值  = 数据脏 → 放行，交给断言层
"""

import unittest
from datetime import date

from app.ds.mapping import Mapping, MappingError


def _mapping(fields):
    return Mapping({
        "name": "case",
        "tables": {"defect_logs": {"source": "rec.csv", "fields": fields}},
    })


class MappingTest(unittest.TestCase):
    def test_rename_and_scale(self):
        m = _mapping({
            "device_id": {"from": "equip_no"},
            "confidence": {"from": "conf_pct", "type": "real", "scale": 0.01},
        })
        out = m.apply("defect_logs", [{"equip_no": "CX-101", "conf_pct": "91"}])
        self.assertEqual(out[0]["device_id"], "CX-101")
        self.assertEqual(out[0]["confidence"], 0.91)

    def test_date_with_format(self):
        m = _mapping({
            "install_date": {"from": "install_dt", "type": "date", "format": "%Y/%m/%d"},
        })
        out = m.apply("defect_logs", [{"install_dt": "2025/03/12"}])
        self.assertEqual(out[0]["install_date"], date(2025, 3, 12))

    def test_bool_yn(self):
        m = _mapping({"is_actually_ok": {"from": "is_ok", "type": "bool"}})
        out = m.apply("defect_logs", [{"is_ok": "Y"}, {"is_ok": "N"}])
        self.assertIs(out[0]["is_actually_ok"], True)
        self.assertIs(out[1]["is_actually_ok"], False)

    def test_empty_value_passes_through(self):
        m = _mapping({"device_id": {"from": "equip_no"}})
        out = m.apply("defect_logs", [{"equip_no": ""}, {"equip_no": "   "}])
        self.assertEqual(out[0]["device_id"], "")
        self.assertEqual(out[1]["device_id"], "")

    def test_default_fills_empty(self):
        m = _mapping({"batch": {"from": "lot_no", "default": ""}})
        out = m.apply("defect_logs", [{"lot_no": ""}])
        self.assertEqual(out[0]["batch"], "")

    def test_missing_column_raises_with_name(self):
        m = _mapping({"device_id": {"from": "equip_no"}})
        with self.assertRaises(MappingError) as ctx:
            m.apply("defect_logs", [{"prod_line": "3号线"}])
        msg = str(ctx.exception)
        self.assertIn("equip_no", msg)
        self.assertIn("第1行", msg)

    def test_bad_value_reports_lineno_and_raw(self):
        m = _mapping({
            "install_date": {"from": "install_dt", "type": "date", "format": "%Y/%m/%d"},
        })
        with self.assertRaises(MappingError) as ctx:
            m.apply("defect_logs", [{"install_dt": "2025/03/12"}, {"install_dt": "2025/13/45"}])
        msg = str(ctx.exception)
        self.assertIn("第2行", msg)
        self.assertIn("2025/13/45", msg)

    def test_extra_source_column_ignored(self):
        m = _mapping({"device_id": {"from": "equip_no"}})
        out = m.apply("defect_logs", [{"equip_no": "CX-101", "remark": "客户备注"}])
        self.assertEqual(set(out[0]), {"device_id"})

    def test_unknown_table_raises(self):
        m = _mapping({"device_id": {"from": "equip_no"}})
        with self.assertRaises(MappingError):
            m.apply("devices", [])


if __name__ == "__main__":
    unittest.main()
