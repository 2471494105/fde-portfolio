"""静默窗口的纯函数：解析、跨午夜、半开边界、零长度不算静默。

这些函数错一个字符，后果都是同一个：值班的人以为今晚没消息，其实是被自己配的窗口吞了。
所以边界逐个钉死，跨午夜单独一条（`22:00-08:00` 是最容易写反的那种）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "raise_alerts", os.path.join(ROOT, "scripts", "raise_alerts.py"))
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)


def at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 11, 27, int(h), int(m))


class ParseQuiet(unittest.TestCase):
    def test_empty_means_off(self):
        self.assertIsNone(ra.parse_quiet(""))
        self.assertIsNone(ra.parse_quiet("   "))

    def test_cross_midnight(self):
        self.assertEqual(ra.parse_quiet("22:00-08:00"), (time(22, 0), time(8, 0)))

    def test_same_day(self):
        self.assertEqual(ra.parse_quiet("09:00-17:30"), (time(9, 0), time(17, 30)))

    def test_bad_spec_raises(self):
        # `time.fromisoformat` 太宽容（2200 / 22.00 都收），所以这里专门卡 HH:MM：
        # 「窗口写错了却看着跑通了」等于把消息悄悄吞掉，是最坏的一种失败。
        for bad in ("2200", "22:00", "22.00-08.00", "22:00-8:00", "8:00-17:00",
                    "22:00-25:00", "22:00-08:00-12:00"):
            with self.assertRaises(ValueError):
                ra.parse_quiet(bad)


class InQuiet(unittest.TestCase):
    def test_cross_midnight_boundaries(self):
        spec = "22:00-08:00"
        self.assertTrue(ra.in_quiet(at("23:30"), spec))
        self.assertTrue(ra.in_quiet(at("22:00"), spec), "起点含在窗口内")
        self.assertTrue(ra.in_quiet(at("07:59"), spec))
        self.assertFalse(ra.in_quiet(at("08:00"), spec), "终点不含，08:00 该开始吵人")
        self.assertFalse(ra.in_quiet(at("12:00"), spec))
        self.assertFalse(ra.in_quiet(at("21:59"), spec))

    def test_same_day_boundaries(self):
        spec = "09:00-17:00"
        self.assertTrue(ra.in_quiet(at("09:00"), spec))
        self.assertTrue(ra.in_quiet(at("16:59"), spec))
        self.assertFalse(ra.in_quiet(at("17:00"), spec))
        self.assertFalse(ra.in_quiet(at("08:59"), spec))

    def test_zero_length_is_not_quiet(self):
        self.assertFalse(ra.in_quiet(at("10:00"), "10:00-10:00"),
                         "零长度窗口不能变成「24 小时不打扰」")

    def test_no_spec(self):
        self.assertFalse(ra.in_quiet(at("23:30"), ""))
        self.assertFalse(ra.in_quiet(at("23:30"), None))

    def test_tuple_input(self):
        self.assertTrue(ra.in_quiet(at("23:30"), (time(22, 0), time(8, 0))))


if __name__ == "__main__":
    unittest.main()
