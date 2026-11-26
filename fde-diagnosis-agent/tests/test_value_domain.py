"""S5 值域抽查单测：把「只改数据不改 schema」的四类变更钉成断言。

每条都成对：正例（干净数据不报）+ 反例（真改了必须报，且档位对）。
"""

import tempfile
import unittest
from pathlib import Path

from app.ds import value_domain as vd


def _csv(rows, header):
    p = Path(tempfile.mkdtemp()) / "probe.csv"
    lines = [",".join(header)] + [",".join(r) for r in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestRange(unittest.TestCase):
    DOM = {"range": [0, 100]}

    def test_clean(self):
        self.assertEqual(vd.check_range("c", ["91", "88", "75"], self.DOM), [])

    def test_percent_to_fraction_is_breaking(self):
        f = vd.check_range("conf_pct", ["0.91", "0.88"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.BREAKING)
        self.assertIn("缩小 100 倍", f[0]["message"])

    def test_dirty_value_does_not_mask_scale_change(self):
        """T12 的核心：库里躺一个 150（÷100 后是 1.5）不许把真实量纲跳变洗成「只是超范围」。

        旧写法按 max 判，1.5 > 1 就判不出来；现在按中位数，0.88 照样在 (0,1] 里。
        """
        vals = ["0.91", "0.88", "0.75", "0.9", "1.5", "-0.1", "0.92", "0.8", "0.81", "0.87"]
        f = vd.check_range("conf_pct", vals, self.DOM)
        self.assertEqual(f[0]["severity"], vd.BREAKING)
        self.assertIn("缩小 100 倍", f[0]["message"])
        # 量纲拒跑的同时，越界值仍单独报出来——一种问题不许吞掉另一种
        self.assertEqual([x["severity"] for x in f[1:]], [vd.RED])

    def test_dirty_value_alone_is_not_scale_evidence(self):
        """成对的另一半：量纲没变、只有脏值，不许拒跑，否则每次同步都误拦。"""
        vals = ["91", "88", "75", "90", "150", "-10", "92", "80", "81", "87"]
        f = vd.check_range("conf_pct", vals, self.DOM)
        self.assertNotIn(vd.BREAKING, {x["severity"] for x in f})
        self.assertEqual(f[0]["severity"], vd.RED)
        self.assertNotIn("量纲疑似", f[0]["message"])

    def test_single_huge_value_is_not_magnify(self):
        """一个 9999 不许把整列判成放大 100 倍：中位数 91 远不到 1000。"""
        vals = ["91", "88", "75", "90", "9999", "92", "80", "81", "87", "86"]
        f = vd.check_range("c", vals, self.DOM)
        self.assertNotIn(vd.BREAKING, {x["severity"] for x in f})

    def test_one_row_still_judged(self):
        """n=1 不加护栏：中位数就是该值本身，与旧写法同风险面，不为好看而放宽。"""
        f = vd.check_range("c", ["0.5"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.BREAKING)

    def test_magnify_is_breaking(self):
        f = vd.check_range("c", ["9100", "8800"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.BREAKING)
        self.assertIn("放大", f[0]["message"])

    def test_out_of_range_is_red_by_default(self):
        f = vd.check_range("c", ["91", "150", "-10"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.RED)
        self.assertEqual(f[0]["samples"], [150.0, -10.0])

    def test_range_severity_downgrade_is_respected(self):
        dom = dict(self.DOM, range_severity="yellow")
        f = vd.check_range("c", ["91", "150"], dom)
        self.assertEqual(f[0]["severity"], vd.YELLOW)

    def test_no_range_declared_means_no_check(self):
        """日期列、枚举列不该被拿去比数值——这是跑真数据时抓到的自己的 bug。"""
        self.assertEqual(vd.check_range("install_dt", ["2025/03/12"], {"format": "%Y/%m/%d"}), [])

    def test_non_numeric_value(self):
        f = vd.check_range("c", ["91", "abc"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.RED)
        self.assertIn("不是数值", f[0]["message"])


class TestEnum(unittest.TestCase):
    DOM = {"enum": ["NG", "OK"]}

    def test_clean(self):
        self.assertEqual(vd.check_enum("judge", ["NG", "OK"], self.DOM), [])

    def test_new_value_is_yellow_with_list(self):
        f = vd.check_enum("judge", ["NG", "RECHECK"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.YELLOW)
        self.assertEqual(f[0]["samples"], ["RECHECK"])


class TestFormat(unittest.TestCase):
    DOM = {"format": "%Y/%m/%d"}

    def test_clean(self):
        self.assertEqual(vd.check_format("dt", ["2025/03/12"], self.DOM), [])

    def test_whole_column_switched_is_breaking(self):
        f = vd.check_format("dt", ["2025-03-12", "2025-04-01"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.BREAKING)
        self.assertIn("%Y-%m-%d", f[0]["message"])

    def test_partial_mismatch_is_red(self):
        f = vd.check_format("dt", ["2025/03/12", "2025-04-01"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.RED)

    def test_garbage_is_breaking(self):
        f = vd.check_format("dt", ["not-a-date"], self.DOM)
        self.assertEqual(f[0]["severity"], vd.BREAKING)


class TestEmpty(unittest.TestCase):
    def test_allow_empty_is_note(self):
        f = vd.check_empty("equip_no", ["", "cx-101"], {"allow_empty": True})
        self.assertEqual(f[0]["severity"], vd.NOTE)

    def test_not_allowed_is_yellow(self):
        f = vd.check_empty("equip_no", ["", "cx-101"], {})
        self.assertEqual(f[0]["severity"], vd.YELLOW)

    def test_no_empty_no_finding(self):
        self.assertEqual(vd.check_empty("c", ["a", "b"], {}), [])


class TestAnalyzeOnRealSamples(unittest.TestCase):
    def test_real_registry_and_real_csv(self):
        """登记必须对得上仓库里的真样本：干净到只该有「夹具越界」和「空值」两条。"""
        dev = vd.analyze("client_a", "devices")
        self.assertEqual(dev["exit"], 0, dev["findings"])
        self.assertEqual([f["column"] for f in dev["findings"]], ["equip_no"])

        logs = vd.analyze("client_a", "defect_logs")
        self.assertEqual(logs["exit"], 0, logs["findings"])
        self.assertEqual([(f["column"], f["severity"]) for f in logs["findings"]],
                         [("conf_pct", "yellow")])

    def test_quantum_shift_on_real_shape_is_breaking(self):
        p = _csv([["1", "0.91", "CX-101", "2号工位", "划伤", "B20261101", "NG", "N"],
                  ["2", "0.88", "CX-101", "2号工位", "划伤", "B20261101", "NG", "N"]],
                 ["rec_id", "conf_pct", "equip_no", "work_station", "defect_name",
                  "lot_no", "judge", "is_ok"])
        r = vd.analyze("client_a", "defect_logs", path=p)
        self.assertEqual(r["exit"], 3, r["findings"])
        cols = {f["column"] for f in r["findings"] if f["severity"] == "breaking"}
        self.assertIn("conf_pct", cols)

    def test_limit_caps_scan(self):
        p = _csv([[str(i), "91"] for i in range(5)], ["rec_id", "conf_pct"])
        rows, n = vd.observe_values(p, limit=3)
        self.assertEqual(n, 3)
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()