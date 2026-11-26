"""时间轴口径的规则测试：三条名字必须互相说得通，不一致就红。

这里不连库，只钉住「什么算不一致」。真正核对表结构与造探针的那两步，
由 check_ds_incremental 第 [12] / [13] 步跑。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ds.time_axis import (  # noqa: E402
    INGEST_CLOCK_DENY,
    TIME_AXIS,
    axis_issues,
)

# staging.sql 里 stg_defect_logs 的真实列（含系统列）。故意写死，不从库里取：
# 这条测试要证明的是「规则本身对不对」，不是「库现在长什么样」。
STG_DEFECT_LOG_COLS = [
    "id", "ts", "device_id", "station", "defect_type",
    "confidence", "batch", "verdict", "is_actually_ok",
    "_src", "_loaded_at",
]


def good_axis(**over):
    """一条合法登记，按用例覆盖字段。"""
    base = {
        "source_filter_field": "ts",
        "local_watermark_field": "ts",
        "business_time_field": "ts",
        "filter_clock": "same_clock",
        "drop_scenario": "",
        "evidence": "K-3",
    }
    base.update(over)
    return base


class TimeAxisRuleTest(unittest.TestCase):
    def test_current_registry_passes(self):
        """仓库里真在用的那条登记必须过——否则脚本每一轮都会红。"""
        self.assertEqual(axis_issues(TIME_AXIS["defect_logs"], STG_DEFECT_LOG_COLS), [])

    def test_empty_registry_is_red(self):
        for axis in ({}, None):
            with self.subTest(axis=axis):
                self.assertEqual(len(axis_issues(axis)), 1)

    def test_missing_each_field_is_red(self):
        for key in ("source_filter_field", "local_watermark_field", "business_time_field"):
            with self.subTest(key=key):
                out = axis_issues(good_axis(**{key: ""}))
                self.assertEqual(len(out), 1)
                self.assertIn(key, out[0])

    def test_watermark_not_business_time_is_red(self):
        """水位记的不是发生时刻：晚到的补录单会被判已灌过（K-3 同一个错法）。"""
        out = axis_issues(good_axis(local_watermark_field="received_at"))
        # 两条：收到的时刻不在业务时间上；received_at 同时是入库时钟黑名单成员
        self.assertEqual(len(out), 2)

    def test_ingest_clock_is_red(self):
        """把 _loaded_at 当业务时间是最常见的错法，必须单独抓。"""
        out = axis_issues(good_axis(
            source_filter_field="_loaded_at",
            local_watermark_field="_loaded_at",
            business_time_field="_loaded_at",
            filter_clock="same_clock",
        ))
        self.assertEqual(len([x for x in out if "入库时钟" in x]), 3)

    def test_ingest_clock_deny_is_case_insensitive(self):
        out = axis_issues(good_axis(business_time_field="Created_At",
                                    local_watermark_field="Created_At"))
        self.assertTrue(any("入库时钟" in x for x in out))

    def test_illegal_filter_clock_is_red(self):
        out = axis_issues(good_axis(filter_clock="probably_same"))
        self.assertTrue(any("filter_clock" in x for x in out))

    def test_different_names_but_same_clock_is_red(self):
        """源端按 committer 筛、业务时间是 author——名字不同却声称同钟，就是 K-3。"""
        out = axis_issues(good_axis(
            source_filter_field="committer_date",
            business_time_field="author_date",
            local_watermark_field="author_date",
            filter_clock="same_clock",
        ))
        self.assertEqual(len(out), 1)
        self.assertIn("同一时钟", out[0])

    def test_different_clock_without_drop_scenario_is_red(self):
        out = axis_issues(good_axis(
            source_filter_field="committer_date",
            business_time_field="author_date",
            local_watermark_field="author_date",
            filter_clock="later_than_business",
            drop_scenario="",
        ))
        self.assertEqual(len(out), 1)
        self.assertIn("没写丢了什么", out[0])

    def test_different_clock_with_drop_scenario_passes(self):
        self.assertEqual(axis_issues(good_axis(
            source_filter_field="committer_date",
            business_time_field="author_date",
            local_watermark_field="author_date",
            filter_clock="later_than_business",
            drop_scenario="committer 晚于 author：author 早于水位的行会被判已灌过，需按 author 回补",
        )), [])

    def test_missing_column_in_table_is_red(self):
        """登记了一个表里不存在的列：这句话在代码里就是空气。"""
        out = axis_issues(good_axis(business_time_field="created_when",
                                    local_watermark_field="created_when"), STG_DEFECT_LOG_COLS)
        self.assertTrue(any("在表里不存在" in x for x in out))

    def test_probe_row_fields_are_in_deny_or_real(self):
        """探针依赖 _loaded_at 真的在表里，否则「读错钟」这个反例造不出来。"""
        self.assertIn("_loaded_at", STG_DEFECT_LOG_COLS)
        self.assertIn("_loaded_at", INGEST_CLOCK_DENY)


if __name__ == "__main__":
    unittest.main()
