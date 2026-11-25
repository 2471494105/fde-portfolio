"""CSV 行源的契约测试。

测的是这个源该有的三件事：能按主键取、取不到就返回 None（不抛）、
坏掉时 health 说清楚坏在哪。

不测「CSV 能不能被解析」——那太基础；真实的坑是「取不到」被当成
「系统故障」，或者空主键的行悄悄顶掉真数据。
"""

import csv
import tempfile
import unittest
from pathlib import Path

from app.ds.csv import DEVICE_COLUMNS, CsvSource
from app.ds.rows import RowSource


def _write_csv(path: Path, rows, fields=None):
    fields = fields or DEVICE_COLUMNS
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _row(device_id="CX-900", **kw):
    base = {
        "device_id": device_id,
        "line": "5号线",
        "station": "1号工位",
        "camera_model": "TestCam",
        "lens_model": "16mm",
        "light_model": "TestLight",
        "install_date": "2026-01-01",
        "maintenance_cycle_days": "90",
    }
    base.update(kw)
    return base


class TestCsvSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        _write_csv(self.dir / "devices.csv", [
            _row("CX-901"),
            _row("CX-902", lens_model="25mm"),
            _row("", lens_model="空主键"),   # 空主键必须被丢弃
        ])
        self.src = CsvSource(data_dir=str(self.dir))

    def tearDown(self):
        self.tmp.cleanup()

    def test_satisfies_row_source_protocol(self):
        self.assertIsInstance(self.src, RowSource)

    def test_keys_sorted_and_skip_blank(self):
        self.assertEqual(self.src.keys("devices"), ["CX-901", "CX-902"])

    def test_get_by_key_returns_row(self):
        row = self.src.get_by_key("devices", "CX-902")
        self.assertEqual(row["lens_model"], "25mm")
        self.assertEqual(row["line"], "5号线")

    def test_missing_key_returns_none_not_raise(self):
        self.assertIsNone(self.src.get_by_key("devices", "CX-999"))

    def test_health_ok(self):
        self.assertTrue(self.src.health()["ok"])

    def test_health_reports_missing_file(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertFalse(CsvSource(data_dir=str(empty)).health()["ok"])

    def test_missing_key_column_fails_loudly(self):
        _write_csv(self.dir / "no_key.csv", [_row()], fields=["line", "station"])
        with self.assertRaises(ValueError):
            CsvSource(data_dir=str(self.dir)).keys("no_key")

    def test_capabilities_declare_columns(self):
        caps = self.src.capabilities()
        self.assertIn("row_lookup", caps)
        self.assertTrue(any(c.startswith("columns:") for c in caps))


if __name__ == "__main__":
    unittest.main()
