# -*- coding: utf-8 -*-
"""护栏 A 档三项的单测：归属校验 / 建单入参校验 / 观测隔离。

跑法（在项目根目录）：
    venv\\Scripts\\python.exe tests\\test_guardrails.py

只测护栏自己的判断，不连数据库、不发 HTTP、不调模型：
工具实现被替换成假函数，QMS 的 httpx.post 被替换成记录器。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import trace
from app.graph import nodes
from app.tools import workorder


class OwnershipGuardTest(unittest.TestCase):
    """act 里的归属校验：设备号必须是用户提过的。"""

    def setUp(self):
        self._orig = dict(nodes.TOOL_IMPLS)
        self.calls = []

        def fake_impl(**kw):
            self.calls.append(kw)
            return {"ok": True, "device_id": kw.get("device_id")}

        nodes.TOOL_IMPLS["query_device_profile"] = fake_impl

    def tearDown(self):
        nodes.TOOL_IMPLS.clear()
        nodes.TOOL_IMPLS.update(self._orig)

    def _run(self, device_ids, want):
        state = {
            "messages": [{
                "role": "assistant",
                "tool_calls": [{
                    "id": "c1",
                    "function": {
                        "name": "query_device_profile",
                        "arguments": json.dumps({"device_id": want}),
                    },
                }],
            }],
            "device_ids": device_ids,
            "device_id": device_ids[0] if device_ids else "",
            "turn": 1,
        }
        out = nodes.act(state)
        res = json.loads(out["messages"][0]["content"])
        return res, out

    def test_foreign_device_is_blocked_before_call(self):
        res, out = self._run(["CX-101"], "CX-102")
        self.assertIn("error", res)
        self.assertEqual(self.calls, [], "越权设备号绝不能真的落到工具上")
        self.assertEqual(res.get("owned_devices"), ["CX-101"])
        # 工具名仍要记账，否则轨迹里会看不出模型尝试过
        self.assertEqual(out["tool_names"], ["query_device_profile"])

    def test_owned_device_passes_and_case_insensitive(self):
        res, _ = self._run(["CX-101"], "cx-101")
        self.assertNotIn("error", res)
        self.assertEqual(len(self.calls), 1)

    def test_no_owned_device_blocks_self_invented_id(self):
        res, _ = self._run([], "CX-101")
        self.assertIn("error", res)
        self.assertEqual(self.calls, [])

    def test_empty_device_id_is_allowed(self):
        # 全厂查询（query_work_order 不带 device_id）是合法用法，不该被归属校验误伤
        res, _ = self._run(["CX-101"], "")
        self.assertNotIn("error", res)
        self.assertEqual(len(self.calls), 1)


class WorkOrderGuardTest(unittest.TestCase):
    """建单前置校验：不合规的入参不许发出去。"""

    def setUp(self):
        self.posts = []
        self._orig_post = workorder.httpx.post

        class Resp:
            status_code = 200
            text = '{"code":0,"message":"created","data":{"order_id":"WO-1","status":"open","assignee_group":"设备科"}}'

            def json(self):
                return json.loads(self.text)

        def fake_post(*args, **kwargs):
            self.posts.append(kwargs.get("json"))
            return Resp()

        workorder.httpx.post = fake_post
        # 单测不连库：设备存在性换成可控替身，默认"存在"
        self._orig_exists = workorder._device_exists
        workorder._device_exists = lambda did: (True, None)

    def tearDown(self):
        workorder.httpx.post = self._orig_post
        workorder._device_exists = self._orig_exists

    def _call(self, **over):
        base = dict(
            device_id="CX-101",
            root_cause="标定漂移超阈值",
            root_cause_code="calibration_drift",
            severity="high",
            actions=["重新标定"],
            evidence_refs=["11-08 误判率 63.2%"],
        )
        base.update(over)
        return workorder.create_work_order(**base)

    def test_empty_evidence_rejected_without_http(self):
        res = self._call(evidence_refs=[])
        self.assertIn("error", res)
        self.assertTrue(any("evidence_refs" in p for p in res["problems"]))
        self.assertEqual(self.posts, [], "校验没过就不能发请求")

    def test_bad_code_rejected(self):
        res = self._call(root_cause_code="calibration drift")
        self.assertIn("error", res)
        self.assertTrue(any("root_cause_code" in p for p in res["problems"]))
        self.assertEqual(self.posts, [])
        self.assertIn("calibration_drift", res["allowed_root_cause_code"])

    def test_bad_severity_rejected(self):
        res = self._call(severity="严重")
        self.assertIn("error", res)
        self.assertEqual(self.posts, [])

    def test_empty_actions_rejected(self):
        res = self._call(actions=["  "])
        self.assertIn("error", res)
        self.assertEqual(self.posts, [])

    def test_normalized_values_reach_payload(self):
        """大小写/空格归一后进 payload——否则幂等键会被写法差异打散。"""
        res = self._call(device_id=" cx-101 ", severity="High")
        self.assertNotIn("error", res)
        self.assertEqual(len(self.posts), 1)
        body = self.posts[0]
        self.assertEqual(body["device_id"], "CX-101")
        self.assertEqual(body["severity"], "high")
        self.assertEqual(body["root_cause_code"], "calibration_drift")

    def test_valid_order_returns_id(self):
        res = self._call()
        self.assertEqual(res.get("order_id"), "WO-1")
        self.assertNotIn("error", res)

    def test_unknown_device_not_created(self):
        workorder._device_exists = lambda did: (False, None)
        res = self._call(device_id="CX-999")
        self.assertIn("error", res)
        self.assertEqual(res.get("state"), "device_not_found")
        self.assertEqual(self.posts, [])

    def test_db_unavailable_is_distinct_state(self):
        """DB 不可用 != 设备不存在：前者可重试，后者要追问编号。"""
        workorder._device_exists = lambda did: (None, "连接超时")
        res = self._call()
        self.assertIn("error", res)
        self.assertEqual(res.get("state"), "system_unavailable")
        self.assertEqual(self.posts, [])


class TracerIsolationTest(unittest.TestCase):
    """观测设施写不出去时，不能把主流程拖死。"""

    def setUp(self):
        self._orig_dir = trace.RUNS_DIR

    def tearDown(self):
        trace.RUNS_DIR = self._orig_dir

    def test_unwritable_path_does_not_raise(self):
        tmp = Path(tempfile.mkdtemp())
        blocker = tmp / "blocked"
        blocker.write_text("x", encoding="utf-8")
        trace.RUNS_DIR = blocker            # 指向文件，mkdir 必然失败

        t = trace.Tracer("u", model="m")    # 不许抛
        self.assertTrue(t.broken)
        t.write("tool", name="x")           # 不许抛
        t.add_tokens(10)
        t.close()                           # 不许抛
        trace.emit("after_close", ok=True)  # contextvar 为空时的静默路径

    def test_normal_roundtrip_writes_three_lines(self):
        tmp = Path(tempfile.mkdtemp())
        trace.RUNS_DIR = tmp

        t = trace.Tracer("u", model="m")
        t.write("tool", name="x")
        t.close()
        lines = t.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3, "run_start / tool / run_end")
        self.assertFalse(t.broken)
        rec = json.loads(lines[-1])
        self.assertEqual(rec["event"], "run_end")
        self.assertEqual(rec["tokens_total"], 0)


class ClaimContractTest(unittest.TestCase):
    """结构化契约（档 B）：结论输出成 claims 数组，一个数字一条，各自带 ref。

    这条路的价值是把「引用」从文本解析搬到结构字段，所以三条必须成立：
      一、ref 对得上 → 通过；ref 写 null → 放行但标灰
      二、ref 指不到字段 / 字段值不符 → 判失败（伪引用）
      三、模型没按契约输出时退回文本解析，不能丢结论
    第三条是关键：只测"契约生效"的实现，会在模型格式抖动时整条丢结论。
    """

    GROUPS = ('{"false_positive_rate": 0.1541, "groups": ['
              '{"key": "1号工位", "count": 34, "rate": 0.5882}]}')

    def _verify(self, doc, *payloads):
        final = doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
        ev = [{"result": p, "turn": 1} for p in payloads]
        state = {"final": final, "evidence": ev, "turn": 1,
                 "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("claim", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_matching_ref_is_cited(self):
        res = self._verify({"conclusion": "1 号工位偏高", "claims": [
            {"text": "误判率 58.82%", "value": 58.82, "unit": "%",
             "ref": "1#groups[0].rate"}]}, self.GROUPS)
        self.assertEqual(res["blocked"], [])
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["cited_ok"], 1)
        # 小数 ↔ 百分比的换算是这里最容易错的一环，单独断言
        self.assertGreaterEqual(res["cite_stats"]["path_ok"], 1)

    def test_null_ref_is_grey_not_fatal(self):
        # 自己定的验收目标：有数字、没出处 → 放行但标灰
        res = self._verify({"conclusion": "复位后应回落", "claims": [
            {"text": "预计降到 6%", "value": 6, "unit": "%", "ref": None}]},
            self.GROUPS)
        self.assertEqual(len(res["blocked"]), 1)
        self.assertEqual(res["misquoted"], [])
        self.assertIn("[无出处]", res["final_text"])

    def test_missing_field_is_misquoted(self):
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "判定 34 条", "value": 34, "unit": "条",
             "ref": "1#groups[0].cnt"}]}, self.GROUPS)
        self.assertTrue(any("34 条" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["path_miss"], 1)

    def test_value_mismatch_is_misquoted(self):
        # 字段存在但值不对：这是"写出处但写错"，比没写出处严重
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "判定 99 条", "value": 99, "unit": "条",
             "ref": "1#groups[0].count"}]}, self.GROUPS)
        self.assertTrue(any("99 条" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["path_bad"], 1)

    def test_ref_to_text_field_matches_number_inside(self):
        # 工具返回的评估说明是整段文字（"上升 10%-25%"），模型引用这段里的 25。
        # 字段不是数字，但里面有这个数——不能因为类型不是 float 就判成引用写错。
        res = self._verify({"conclusion": "光源超寿命", "claims": [
            {"text": "误判率通常上升 10%~25%", "value": 25, "unit": "%",
             "ref": "1#assessment"}]},
            '{"assessment": "已超额定寿命 620 小时，上升 10%-25%，建议更换光源"}')
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["path_ok"], 1)

    def test_no_json_is_failure_not_silent_pass(self):
        # 旧文本解析路径已删。模型不按契约输出时判失败，不再静默退回——
        # 退回等于"这条没校验"，而没校验的结论被当成通过，才是真正危险的地方。
        res = self._verify("标定周期 90 天[1#maintenance_cycle_days]。",
                           '{"maintenance_cycle_days": 90}')
        self.assertGreaterEqual(res["cite_stats"]["json_bad"], 1)
        self.assertTrue(any("契约" in m for m in res["misquoted"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
