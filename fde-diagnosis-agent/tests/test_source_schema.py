"""源端登记层的单测：观测、比对、拒跑三级。

重点不是"函数能跑通"，而是把卡 2 里写下的反例期望钉成断言：
  - 加一列 → 报出来（不能静默）
  - 删主键 → 拒跑
  - 一加一减 → 判为改名嫌疑，除非 renames 里声明过
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.ds import source_schema as ss


def _write_csv(dirpath, header, name="probe.csv"):
    p = Path(dirpath) / name
    p.write_text(",".join(header) + "\n", encoding="utf-8")
    return p


class TestObserve(unittest.TestCase):
    def test_csv_header(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_csv(d, ["equip_no", "prod_line"])
            obs = ss.observe_csv_header(p)
        self.assertEqual(sorted(obs), ["equip_no", "prod_line"])
        self.assertIsNone(obs["equip_no"]["type"])

    def test_csv_bom_and_blank_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bom.csv"
            p.write_text("\ufeffequip_no,prod_line\n", encoding="utf-8")
            self.assertEqual(sorted(ss.observe_csv_header(p)), ["equip_no", "prod_line"])
            bad = _write_csv(d, ["equip_no", ""], "bad.csv")
            with self.assertRaises(ValueError):
                ss.observe_csv_header(bad)

    def test_odoo_fields(self):
        text = (
            "name = fields.Char('Name', required=True)\n"
            "sequence = fields.Integer()\n"
            "    note = fields.Html('Notes')\n"
            "not_a_field = 1\n"
        )
        got = ss.parse_odoo_fields(text)
        self.assertEqual(sorted(got), ["name", "note", "sequence"])
        self.assertTrue(got["name"]["required"])
        self.assertEqual(got["sequence"]["type"], "Integer")
        self.assertFalse(got["note"]["required"])

    def test_information_schema(self):
        rows = [("id", "bigint", "NO"), ("memo", "text", "YES")]
        got = ss.parse_information_schema(rows)
        self.assertTrue(got["id"]["required"])
        self.assertFalse(got["memo"]["required"])
        self.assertEqual(got["memo"]["type"], "text")

    def test_response_keys_first_level_only(self):
        got = ss.parse_response_keys([{"id": 1, "meta": {"a": 1}}])
        self.assertEqual(sorted(got), ["id", "meta"])  # 嵌套对象不展开
        with self.assertRaises(ValueError):
            ss.parse_response_keys("not json")


REG = {
    "client": "client_a",
    "table": "devices",
    "key_columns": ["equip_no"],
    "renames": {},
    "columns": [{"name": "equip_no"}, {"name": "prod_line"}, {"name": "cam_model"}],
}


class TestDiff(unittest.TestCase):
    def test_unchanged(self):
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["equip_no", "prod_line", "cam_model"]))
        d = ss.diff(REG, obs)
        self.assertTrue(d["clean"])
        self.assertEqual(d["removed"], [])
        self.assertEqual(ss.breaking(d, REG), [])

    def test_added_column_is_red_not_breaking(self):
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["equip_no", "prod_line", "cam_model", "torque"]))
        d = ss.diff(REG, obs)
        self.assertEqual(d["added"], ["torque"])
        self.assertFalse(d["clean"])
        self.assertEqual(ss.breaking(d, REG), [])

    def test_removed_non_key_is_red_only(self):
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["equip_no", "prod_line"]))
        d = ss.diff(REG, obs)
        self.assertEqual(d["removed"], ["cam_model"])
        self.assertEqual(ss.breaking(d, REG), [])

    def test_removed_key_breaks(self):
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["prod_line", "cam_model"]))
        d = ss.diff(REG, obs)
        reasons = ss.breaking(d, REG)
        self.assertEqual(len(reasons), 1)
        self.assertIn("equip_no", reasons[0])

    def test_type_and_required_change(self):
        reg = dict(REG, columns=[{"name": "equip_no", "type": "bigint", "required": False},
                                 {"name": "prod_line", "type": "text"}])
        obs = {"equip_no": {"name": "equip_no", "type": "text", "required": True},
               "prod_line": {"name": "prod_line", "type": "text"}}
        d = ss.diff(reg, obs)
        self.assertEqual(d["type_changed"][0]["column"], "equip_no")
        self.assertEqual(d["required_changed"][0]["column"], "equip_no")
        self.assertFalse(d["clean"])

    def test_csv_has_no_types_so_that_dimension_is_unchecked(self):
        reg = dict(REG, columns=[{"name": "equip_no", "type": "bigint"},
                                 {"name": "prod_line", "type": "text"},
                                 {"name": "cam_model", "type": "text"}])
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["equip_no", "prod_line", "cam_model"]))
        d = ss.diff(reg, obs)
        self.assertEqual(sorted(d["type_unchecked"]), ["cam_model", "equip_no", "prod_line"])
        self.assertEqual(d["type_changed"], [])

    def test_undeclared_rename_is_suspect_not_silent(self):
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["equipment_no", "prod_line", "cam_model"]))
        d = ss.diff(REG, obs)
        self.assertEqual(d["added"], ["equipment_no"])
        self.assertEqual(d["removed"], ["equip_no"])
        self.assertIsNotNone(d["rename_suspect"])
        self.assertTrue(ss.breaking(d, REG))  # 改的正是主键列

    def test_declared_rename_is_resolved(self):
        reg = dict(REG, renames={"equipment_no": "equip_no"})
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["equipment_no", "prod_line", "cam_model"]))
        d = ss.diff(reg, obs)
        self.assertTrue(d["clean"])
        self.assertEqual(d["renamed"], [{"from": "equip_no", "to": "equipment_no"}])
        self.assertEqual(ss.breaking(d, REG), [])


class TestRegistryAndConsumption(unittest.TestCase):
    def test_missing_registry_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ss.SourceSchemaNotRegistered) as ctx:
                ss.load("client_a", "no_such_table", root=Path(d))
        self.assertIn("没有源端登记", str(ctx.exception))

    def test_real_registry_matches_real_csv(self):
        """真登记 vs 真文件：这两份登记必须对得上仓库里的客户端样本，否则登记就是假的。"""
        for client, table, src in (
            ("client_a", "devices", "data/sandbox/from_client/equipment.csv"),
            ("client_a", "defect_logs", "data/sandbox/from_client/defect_records.csv"),
        ):
            reg = ss.load(client, table)
            obs = ss.observe_csv_header(ss.ROOT / reg["source_path"])
            d = ss.diff(reg, obs)
            self.assertTrue(d["clean"], f"{table} 登记与现实不符：{d}")
            self.assertEqual(reg["source_path"], src)

    def test_unconsumed_lists_leftover_columns(self):
        obs = ss.observe_csv_header(_write_csv(tempfile.mkdtemp(), ["equip_no", "prod_line", "lamp_type"]))
        self.assertEqual(ss.unconsumed(obs, ["equip_no"]), ["lamp_type", "prod_line"])
        self.assertEqual(ss.unconsumed(obs, ["equip_no", "prod_line", "lamp_type"]), [])


if __name__ == "__main__":
    unittest.main()