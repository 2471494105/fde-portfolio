# -*- coding: utf-8 -*-
"""跑前自检的判定逻辑（scripts/preflight.py 里的纯函数）。

跑法（在项目根目录）：
    venv\\Scripts\\python.exe -m unittest tests.test_preflight -v

刻意只测纯函数，不连库、不开端口：一份"跑前检查"自己也需要能离线验证，
否则它坏了的方式和它要防的事一模一样——静默地放过，或者静默地永远报错。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import preflight

SEED_OK = {"devices": 5, "work_orders": 1, "defect_logs": 1000, "alarms": 8}


class SeedStateTest(unittest.TestCase):
    """库态 vs 种子态。"""

    def test_clean_seed_passes(self):
        self.assertEqual(preflight.seed_problems(SEED_OK, "9caa1322fbfc"), [])

    def test_leftover_work_orders_is_caught(self):
        """提交前实测到的那种：work_orders=4，文档却写着"已复位"。"""
        bad = dict(SEED_OK, work_orders=4)
        msgs = preflight.seed_problems(bad, "9caa1322fbfc")
        self.assertEqual(len(msgs), 1)
        self.assertIn("work_orders=4", msgs[0])
        self.assertIn("002_data.sql", msgs[0])

    def test_seed_changed_is_caught_before_counts(self):
        """种子换了版本，行数期望就失效——不能报成"上一轮没复位"。"""
        msgs = preflight.seed_problems(dict(SEED_OK, defect_logs=9), "deadbeefcafe")
        self.assertGreaterEqual(len(msgs), 2)
        self.assertIn("seed_hash 变成 deadbeefcafe", msgs[0])
        self.assertIn("defect_logs=9", msgs[1])

    def test_counts_mismatch_reports_every_field(self):
        """不是"报第一条就停"：一次把四张表都清点出来，省得改一条再来一次。"""
        bad = {"devices": 5, "work_orders": 4, "defect_logs": 9, "alarms": 3}
        msgs = preflight.seed_problems(bad, "9caa1322fbfc")
        self.assertEqual(len(msgs), 3)


class CoverageTest(unittest.TestCase):
    """事实层缺口。"""

    def test_no_gap_passes(self):
        self.assertEqual(preflight.coverage_problems(35, []), [])

    def test_gap_is_named(self):
        msgs = preflight.coverage_problems(35, ["CX-105.line", "CX-101.install_date"])
        self.assertEqual(len(msgs), 1)
        self.assertIn("CX-105.line", msgs[0])
        self.assertIn("N-16", msgs[0])

    def test_gap_list_is_truncated(self):
        """缺口很多时不刷屏，但要明说还有更多。"""
        many = [f"CX-10{i%10}.line" for i in range(20)]
        msg = preflight.coverage_problems(35, many)[0]
        self.assertIn("…", msg)
        self.assertEqual(msg.count("CX-10"), 6)


class ScriptShapeTest(unittest.TestCase):
    """自检脚本本身的两条硬约束。"""

    def test_not_in_behavior_files(self):
        """它不在指纹里，所以加它不会让 1547 那份全量作废。

        这条不是风格问题：`BEHAVIOR_FILES` 里任何一份改了，`code_hash` 就变，
        磁盘上那份同指纹全量立刻失效。为"跑前自检"这样一个只读功能付一次全量，
        是这笔账里最不划算的一种。
        """
        from app.meta import BEHAVIOR_FILES
        self.assertNotIn("scripts/preflight.py", BEHAVIOR_FILES)

    def test_qms_requirement_matches_failure_message(self):
        """--skip-qms 只去掉 8010，别的端口一个都不许少。"""
        ports = [p for p, _ in preflight.PORTS]
        self.assertIn(8010, ports)
        kept = [p for p in ports if p != 8010]
        self.assertEqual(kept, [5432, 11434])


if __name__ == "__main__":
    unittest.main()
