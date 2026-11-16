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


class DerivedDayGapTest(unittest.TestCase):
    """天数由证据里相邻两次记录的日期相减得出时，应放行并计入 derived。

    四条断言必须同时成立，少一条都会被宽泛实现骗过：
    - 只证"能放行"：一个"见到天就放行"的实现也能过；
    - 只证"算错要拦"：一个"永远拦"的实现也能过；
    - 只有一端日期：证明放行不是无条件的；
    - 首末跨度：证明用的是相邻差，不是任意两两相减（否则 10 条记录能凑出几乎任意天数）。
    """

    RECS = (
        '{"records": ['
        '{"ts": "2026-11-07 08:30:00"}, '
        '{"ts": "2026-10-26 09:30:00"}, '
        '{"ts": "2026-09-20 14:00:00"}, '
        '{"ts": "2026-08-12 10:00:00"}]}'
    )

    def _verify(self, final, payload):
        state = {"final": final, "evidence": [{"result": payload, "turn": 1}],
                 "turn": 1, "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("day-gap", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_adjacent_date_gap_is_derived_not_blocked(self):
        res = self._verify("08-12 到 09-20 间隔 39 天[1]。", self.RECS)
        self.assertEqual(res["blocked"], [])
        self.assertGreaterEqual(res["cite_stats"]["derived"], 1)

    def test_gap_with_only_one_date_is_blocked(self):
        res = self._verify("两次标定间隔 39 天[1]。",
                           '{"records": [{"ts": "2026-11-07 08:30:00"}]}')
        self.assertIn("39天[1]", res["blocked"])

    def test_wrong_gap_value_is_blocked(self):
        res = self._verify("两次标定间隔 45 天[1]。", self.RECS)
        self.assertIn("45天[1]", res["blocked"])

    def test_span_across_records_is_not_a_gap(self):
        res = self._verify("首末两次相隔 87 天[1]。", self.RECS)
        self.assertIn("87天[1]", res["blocked"])


class ExprChannelTest(unittest.TestCase):
    """写了完整算式的数量应当放行，无论百分比还是倍率，也无论分子分母是否同源。

    四条必须同时成立。最后一条是关键：没有"算式算错要拦"，
    一个"见到倍就放行"的实现照样能过前三条。
    """

    def _verify(self, final, *payloads):
        ev = [{"result": p, "turn": 1} for p in payloads]
        state = {"final": final, "evidence": ev, "turn": 1,
                 "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("expr", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_multiplier_with_expression_is_allowed(self):
        res = self._verify("实测漂移是限值的 1.4 倍（=0.42/0.3）。",
                           '{"drift_mm": 0.42}', '{"limit_mm": 0.3}')
        self.assertEqual(res["blocked"], [])

    def test_multiplier_without_expression_is_blocked(self):
        res = self._verify("实测漂移是限值的 1.4 倍。",
                           '{"drift_mm": 0.42}', '{"limit_mm": 0.3}')
        self.assertIn("1.4倍", res["blocked"])

    def test_expression_across_two_evidence_is_allowed(self):
        res = self._verify("漂移 0.42mm 是限值的 1.4 倍（=0.42/0.3）[1][2]。",
                           '{"drift_mm": 0.42}', '{"limit_mm": 0.3}')
        self.assertEqual(res["blocked"], [])

    def test_wrong_expression_is_blocked(self):
        res = self._verify("实测漂移是限值的 1.4 倍（=0.42/0.9）。",
                           '{"drift_mm": 0.42}', '{"limit_mm": 0.3}')
        self.assertIn("1.4倍", res["blocked"])


class CountChannelTest(unittest.TestCase):
    """「最近 5 次标定」里的 5 是数数组长度数出来的，不是抄的，应当放行。

    四条：数对了放行 / 数错了拦 / 非计数单位不适用 / 嵌套数组也数得到。
    第三条是防"任何数都拿数组长度解释"。
    """

    RECS5 = '{"device_id": "CX-104", "records": [{},{},{},{},{}]}'

    def _verify(self, final, payload):
        state = {"final": final, "evidence": [{"result": payload, "turn": 1}],
                 "turn": 1, "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("count", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_count_matching_array_length_is_allowed(self):
        res = self._verify("最近 5 次标定全部通过[1]。", self.RECS5)
        self.assertEqual(res["blocked"], [])
        self.assertGreaterEqual(res["cite_stats"]["counted"], 1)

    def test_wrong_count_is_blocked(self):
        res = self._verify("最近 6 次标定全部通过[1]。", self.RECS5)
        self.assertIn("6次[1]", res["blocked"])

    def test_count_channel_not_applied_to_length_units(self):
        res = self._verify("标定漂移 5mm[1]。", self.RECS5)
        self.assertIn("5mm[1]", res["blocked"])

    def test_nested_array_length_is_found(self):
        res = self._verify("最近 5 次标定全部通过[1]。",
                           '{"data": {"records": [{},{},{},{},{}]}}')
        self.assertEqual(res["blocked"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
