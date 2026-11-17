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
from app.graph import build, nodes
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

    def _verify(self, doc, *payloads, truncated=False):
        final = doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
        ev = [{"result": p, "turn": 1, "truncated": truncated} for p in payloads]
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


class RangeClaimTest(unittest.TestCase):
    """区间 claim（`6%~11%`、`2–4 条`）：用一个 value 塞不下，得写 value_max。

    改之前 value 只能装下界，上界根本不参与校验——
    "预计降到 6%~99%" 配 value=6 能全过。

    这条设计的核心不是"加了个字段"，是**合法性取决于 ref 指向什么**：
      ref 指整段文字（原文里就写着「上升 10%-25%」）→ 两端都能在原文找到，合法
      ref 指数值字段（如 0.1541）→ 这个位置只担保一个精确值，撑不住区间，判失败
      ref 为 null → 模型自己归纳的区间，标灰放行
    """

    RANGE_TEXT = '{"assessment": "已超额定寿命 620 小时，上升 10%-25%", "rate": 0.1541}'

    def _verify(self, doc, *payloads):
        final = json.dumps(doc, ensure_ascii=False)
        ev = [{"result": p, "turn": 1, "truncated": False} for p in payloads]
        state = {"final": final, "evidence": ev, "turn": 1,
                 "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("range", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_range_from_text_field_is_cited(self):
        # 原文里本来就写着「上升 10%-25%」，两端都能找到 → 合法引用
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "上升 10%~25%", "value": 10, "value_max": 25, "unit": "%",
             "ref": "1#assessment"}]}, self.RANGE_TEXT)
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["range_ok"], 1)
        self.assertEqual(res["cite_stats"]["range_bad"], 0)

    def test_range_on_numeric_field_is_misquoted(self):
        # 反向断言：ref 指着一个精确数值（0.1541 = 15.41%），
        # 却用它担保 10%~25%——那个上界不是这个位置给的。必须判失败。
        # 只要实现里少了"数值字段禁 value_max"这条，这一行就会挂。
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "误判率 10%~25%", "value": 10, "value_max": 25, "unit": "%",
             "ref": "1#rate"}]}, self.RANGE_TEXT)
        self.assertTrue(any("撑不住区间" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["range_bad"], 1)

    def test_range_missing_one_endpoint_is_misquoted(self):
        # 只找到一端不算：另一端是模型自己加的，不是原文里的
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "上升 10%~40%", "value": 10, "value_max": 40, "unit": "%",
             "ref": "1#assessment"}]}, self.RANGE_TEXT)
        self.assertTrue(any("字段原文对不上" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["range_bad"], 1)

    def test_range_with_null_ref_is_grey_not_fatal(self):
        # 自己归纳的区间（不是从原文抄的）：标灰放行，不计失败
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "预计降到 6%~11%", "value": 6, "value_max": 11, "unit": "%",
             "ref": None}]}, self.RANGE_TEXT)
        self.assertEqual(res["misquoted"], [])
        self.assertEqual(len(res["blocked"]), 1)
        self.assertIn("[无出处]", res["final_text"])

    def test_reversed_bounds_is_misquoted(self):
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "上升 25%~10%", "value": 25, "value_max": 10, "unit": "%",
             "ref": "1#assessment"}]}, self.RANGE_TEXT)
        self.assertTrue(any("上下界写反" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["range_bad"], 1)


class CheckLoopTest(unittest.TestCase):
    """结论交出前的自查闸口：verify 从"终点"变成"能回头的检查点"。

    依据是 Claude Agent SDK 的 gather context → take action → verify work → repeat，
    以及同页那句「providing clearly defined rules for an output, then explaining
    which rules failed and why」——verify 的产出是失败原因，不是布尔。

    改之前 verify 在循环外，判完就终局。11-17 那次 H1：模型把 2/3 号工位的 rate
    抄反了，_verify_claims 准确抓到「字段值 0.1139 ≠ 4.84%」，但抓到时已在终点，
    只能判 FAIL。同一句话当时回喂过去，它自己能改。

    四条必须成立：
      一、干净的结论不回环（否则每次都多花一轮调用）
      二、伪引用回环一次，回喂文案里有具体值，不是"引用有误"
      三、已经回环过就不再回环（防死循环）
      四、回环后拿 ref=null 换通过要被记下来——这是新开的作弊面
    """

    PAYLOAD = '{"rate": 0.1139, "ok_rate": 0.0484}'

    def _run_check(self, final, retry=0, notes=None, unsourced0=0):
        st = {"final": final, "check_retry": retry, "check_notes": list(notes or []),
              "check_unsourced0": unsourced0,
              "evidence": [{"result": self.PAYLOAD, "turn": 1, "truncated": False}],
              "messages": []}
        return nodes.check(st)

    def test_clean_claim_passes_without_retry(self):
        out = self._run_check(json.dumps({"conclusion": "x", "claims": [
            {"text": "2 号工位误判率 11.39%", "value": 11.39, "unit": "%",
             "ref": "1#rate"}]}, ensure_ascii=False))
        self.assertEqual(out["check_action"], "pass")
        self.assertEqual(out["check_retry"], 0)
        self.assertNotIn("messages", out)

    def test_bad_claim_retries_once_with_reason(self):
        out = self._run_check(json.dumps({"conclusion": "x", "claims": [
            {"text": "2 号工位误判率 4.84%", "value": 4.84, "unit": "%",
             "ref": "1#rate"}]}, ensure_ascii=False))
        self.assertEqual(out["check_action"], "retry")
        self.assertEqual(out["check_retry"], 1)
        msg = out["messages"][-1]["content"]
        # 回喂必须带具体值：只说"引用有误"，模型只能猜该改哪个数
        self.assertIn("0.1139", msg)
        self.assertIn("4.84", msg)
        self.assertIn("null", msg)

    def test_second_round_does_not_retry_again(self):
        out = self._run_check(json.dumps({"conclusion": "x", "claims": [
            {"text": "2 号工位误判率 4.84%", "value": 4.84, "unit": "%",
             "ref": "1#rate"}]}, ensure_ascii=False), retry=1)
        self.assertEqual(out["check_action"], "pass")
        self.assertEqual(out["check_retry"], 1)

    def test_null_swap_after_retry_is_flagged(self):
        # 回环前 0 条未溯源；回环后把指错的 ref 全改成 null，misquoted 清零了。
        # 那些数从"指错了"变成"没指"——后者不报错，却比前者更差。
        out = self._run_check(json.dumps({"conclusion": "x", "claims": [
            {"text": "2 号工位误判率 4.84%", "value": 4.84, "unit": "%",
             "ref": None}]}, ensure_ascii=False), retry=1, unsourced0=0)
        self.assertEqual(out["check_action"], "pass")
        self.assertTrue(any("不是修正" in n for n in out["check_notes"]))

    def test_non_json_goes_straight_to_verify(self):
        # 上一轮就没写出合法 JSON，重跑一次大概率还是写不出：不值得为格式再花一轮
        out = self._run_check("这不是 JSON 结论")
        self.assertEqual(out["check_action"], "pass")

    def test_route_reads_check_action(self):
        self.assertEqual(nodes.route_after_check({"check_action": "retry"}), "plan")
        self.assertEqual(nodes.route_after_check({"check_action": "pass"}), "verify")

    def test_check_notes_reach_final_judgement(self):
        # check 抓到的"拿 null 换通过"必须进最终 misquoted。
        # 不并进去的话，它就是个写了没人读的字段，绕法本身静默通过。
        st = {"final": json.dumps({"conclusion": "x", "claims": [
            {"text": "误判率 11.39%", "value": 11.39, "unit": "%",
             "ref": "1#rate"}]}, ensure_ascii=False),
              "evidence": [{"result": self.PAYLOAD, "turn": 1, "truncated": False}],
              "turn": 1, "user_msg": "", "tool_names": [], "steps": 0,
              "check_notes": ["回环后未溯源数字从 0 条涨到 2 条：把指错的 ref 改成 null 不是修正"]}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("chk", model="m", provider="p", base_url="b"):
                res = nodes.verify(st)
        finally:
            trace.RUNS_DIR = old
        self.assertTrue(any("不是修正" in m for m in res["misquoted"]))


class PayloadBudgetTest(unittest.TestCase):
    """证据体积：任何返回都必须压成**合法 JSON**，且不超上限。

    为什么这是硬约束而不是"尽量"：verify 要靠 _load_obj 解析证据取字段。
    一旦不是 JSON，所有引用都判 ref_bad——报出来是"模型引用写错"，
    实际是证据坏了。实测：给 defect_log 加一个聚合维度就到 1606 字，
    改造前那一段直接切字符串，两条完全正确的引用全被判失败。

    下面的测试就是那条链的回归。旧实现（text[:limit] + "…（已截断）"）
    会在 test_oversized_payload_is_valid_json 上直接抛 ValueError。
    """

    LIMIT = 1600

    def _defect_log(self, extra_dims):
        res = {
            "device_id": "CX-104",
            "total": 560,
            "groups": [{"ts": f"2026-11-1{i}T00:00:00", "n": 100 + i,
                        "rate": round(6 + i * 0.14, 2)} for i in range(10)],
            "note": "rate 是误判率百分比；按小时聚合，空档未记 0",
        }
        if extra_dims:
            res["by_shift"] = [
                {"shift": "夜班 23:00-07:00", "n": 180, "rate": 12.4, "top_line": "4号线 1-2号工位"},
                {"shift": "白班 07:00-15:00", "n": 210, "rate": 5.1, "top_line": "2号线 3号工位"},
            ]
            res["by_line"] = [
                {"line": "1号线", "n": 120, "rate": 4.2, "top_station": "1-1号工位", "top_type": "划痕"},
                {"line": "4号线", "n": 200, "rate": 14.1, "top_station": "4-2号工位", "top_type": "漏检"},
            ]
            res["by_defect_type"] = [
                {"type": "划痕", "n": 160, "rate": 6.1},
                {"type": "漏检", "n": 130, "rate": 9.2},
            ]
            res["by_batch"] = [
                {"batch": "B2026-11-16-A", "n": 58, "rate": 13.1, "note": "已超额定寿命，未更换"},
            ]
            res["caveat"] = "工位归属以缺陷日志的 station 字段为准，历史数据存在少量缺失"
        return res

    def test_oversized_payload_is_valid_json(self):
        # 结构压不下来（400 个独立短字段）→ 走降级包装，仍必须是 JSON
        flat = {f"k{i:03d}": f"值{i}" for i in range(400)}
        text, cut = nodes._payload(flat)
        self.assertTrue(cut)
        self.assertLessEqual(len(text), self.LIMIT)
        obj = json.loads(text)           # 旧实现到这里抛 ValueError
        self.assertTrue(obj["_payload_overflow"])

    def test_structure_shrink_beats_char_cut(self):
        # 多个聚合维度：应该靠逐档收紧结构救回来，而不是切字符串
        text, cut = nodes._payload(self._defect_log(extra_dims=True))
        obj = json.loads(text)
        self.assertFalse(cut, "能靠结构压下来时不该走降级")
        self.assertEqual(obj["by_shift"][0]["rate"], 12.4, "关键字段必须还在")
        self.assertLessEqual(len(text), self.LIMIT)

    def test_cut_evidence_is_grey_not_misquoted(self):
        # 证据被压到装不下 → 字段不在里面。模型写的路径也许是对的，
        # 判"引用写错"是误判，应当放行标灰。
        ev_text, cut = nodes._payload({f"k{i:03d}": f"值{i}" for i in range(400)})
        self.assertTrue(cut)
        doc = {"conclusion": "x", "claims": [
            {"text": "判定 34 条", "value": 34, "unit": "条", "ref": "1#groups[0].count"}]}
        final = json.dumps(doc, ensure_ascii=False)
        state = {"final": final, "evidence": [{"result": ev_text, "turn": 1, "truncated": True}],
                 "turn": 1, "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("cut", model="m", provider="p", base_url="b"):
                res = nodes.verify(state)
        finally:
            trace.RUNS_DIR = old
        self.assertEqual(res["misquoted"], [], "证据被压缩不能算模型引用写错")
        self.assertGreaterEqual(res["cite_stats"]["cite_cut"], 1)
        self.assertEqual(len(res["blocked"]), 1)

    def test_uncut_missing_field_still_fails(self):
        # 反向断言：证据没被截断时，字段不存在仍然判失败。
        # 少了这条，把 cite_cut 写成"无条件放行"也能让上一条测试通过。
        res = ClaimContractTest._verify(self, {"conclusion": "x", "claims": [
            {"text": "判定 34 条", "value": 34, "unit": "条",
             "ref": "1#groups[0].cnt"}]}, ClaimContractTest.GROUPS, truncated=False)
        self.assertGreaterEqual(res["cite_stats"]["path_miss"], 1)
        self.assertTrue(any("34 条" in m for m in res["misquoted"]))


class ContextEvidenceAlignmentTest(unittest.TestCase):
    """k 索引的隐式耦合：模型数的第 k 条工具返回，必须正好是 ev[k-1]。

    模型怎么得到 k：数它这一轮 messages 里排第几的 tool 消息。
    校验层怎么取：按 turn 过滤后的 evidence 列表下标。
    两边保留的轮数必须相等，否则 k 整体错位——表现是「引用率突然归零」
    或「大面积 cite_oob」，看不出是配置问题，第一反应会去怀疑模型。

    这个洞之前是空的：scripts/check_ctx.py 只测了裁剪和 tool 配对，
    没有任何断言把「模型看到的」和「参与校验的」绑在一起。
    """

    EV_PER_TURN = 2

    def _state(self, turns):
        msgs = [{"role": "user", "content": "第一句原始诉求"}]
        evidence = []
        for t in range(1, turns + 1):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{
                "id": f"c{t}", "type": "function",
                "function": {"name": "query_device_profile", "arguments": "{}"}}]})
            for j in range(self.EV_PER_TURN):
                msgs.append({"role": "tool", "tool_call_id": f"c{t}",
                             "content": f'{{"turn": {t}, "i": {j}}}'})
                evidence.append({"result": f'{{"turn": {t}, "i": {j}}}', "turn": t})
            msgs.append({"role": "assistant", "content": f"第{t}轮结论"})
            if t < turns:
                msgs.append({"role": "user", "content": f"第{t+1}轮提问"})
        return {"messages": msgs, "evidence": evidence, "turn": turns}

    def test_visible_tool_messages_equal_evidence_used(self):
        # 核心不变式：模型看到的 tool 消息条数 == 参与校验的证据条数。
        # 任何一边改了保留轮数，这条立刻红。
        for turns in (1, 2, 3, 5, 9):
            with self.subTest(turns=turns):
                st = self._state(turns)
                visible = [m for m in nodes._context_messages(st) if m.get("role") == "tool"]

                lo, hi = nodes._ctx_window(turns)
                used = [e for e in st["evidence"] if int(e["turn"]) >= lo]

                self.assertEqual(len(visible), len(used),
                                 f"{turns} 轮时模型能看到 {len(visible)} 条工具返回，"
                                 f"校验只用 {len(used)} 条，k 会错位")

    def test_window_is_contiguous_and_ends_at_current_turn(self):
        # 区间必须是连续的一串，且末端就是当前轮；否则 ev 会漏掉中间一轮，
        # 模型的 k 在第几轮之后就整体偏移。
        for turns in (1, 2, 3, 7):
            lo, hi = nodes._ctx_window(turns)
            self.assertEqual(hi, turns)
            self.assertEqual(hi - lo + 1, min(turns, nodes.CTX_VISIBLE_TURNS))

    def test_first_turn_request_kept_without_its_evidence(self):
        # 窗口外保留了首句原始诉求（只留 user 文本，不留那一轮的工具返回）。
        # 这是有意的：丢掉起点模型就不知道用户最初问什么，
        # 但那一轮的工具返回不该参与溯源，否则等于给旧证据开后门。
        st = self._state(6)
        out = nodes._context_messages(st)
        self.assertEqual(out[0].get("content"), "第一句原始诉求")
        self.assertFalse(any(m.get("content") == '{"turn": 1, "i": 0}' for m in out))

    def test_verify_counts_exactly_the_window(self):
        # verify 的 cite_stats["ev"] 就是它实际拿来校验的证据条数。
        # 断言它等于 _ctx_window 算出的条数。
        # 这条比上一条更硬：上一条在 CTX_VISIBLE_TURNS=2 时，
        # 「verify 里写死 cur_turn-1」和「从 _ctx_window 派生」数值相同，测不出差异；
        # 这一条在常量变成 1 或 3 时立刻暴露写死的那份。
        for turns in (1, 2, 4, 7):
            with self.subTest(turns=turns):
                st = self._state(turns)
                lo, _ = nodes._ctx_window(turns)
                expected = sum(1 for e in st["evidence"] if e["turn"] >= lo)
                st["final"] = json.dumps(
                    {"conclusion": "x", "claims": [], "advice": []}, ensure_ascii=False)
                st["user_msg"] = ""
                st["tool_names"] = []
                st["steps"] = 0

                old = trace.RUNS_DIR
                trace.RUNS_DIR = Path(tempfile.mkdtemp())
                try:
                    with trace.Tracer("win", model="m", provider="p", base_url="b"):
                        res = nodes.verify(st)
                finally:
                    trace.RUNS_DIR = old
                self.assertEqual(res["cite_stats"]["ev"], expected,
                                 f"{turns} 轮：verify 用了 {res['cite_stats']['ev']} 条证据，"
                                 f"窗口应是 {expected} 条")


class BareQuoteRepairTest(unittest.TestCase):
    """结论 JSON 里值内裸引号：修格式，但不放宽校验。

    真实失败样本：advice 里写「无法严格量化"夜班vs白班"差异」，
    外层双引号被当场截断，2427 字符的结论在第 2339 字符处解析失败，
    14 条正确的引用全部作废、语义正确率归零。
    """

    BAD = ('{"conclusion": "x", "claims": [{"text": "58.82%", "value": 58.82, '
           '"unit": "%", "ref": "2#groups[0].rate"}], '
           '"advice": ["无法严格量化"夜班vs白班"差异"]}')

    def test_raw_sample_really_breaks_first(self):
        # 前提断言：不改就是坏的。没有这一条，修复测试可能只是在测空气。
        with self.assertRaises(Exception):
            json.loads(self.BAD)

    def test_repair_keeps_content_and_structure(self):
        doc = nodes._parse_claims(self.BAD)
        self.assertIsNotNone(doc, "修完必须能解析")
        self.assertTrue(doc.get("_repaired"), "走没走修复通道要留痕")
        self.assertEqual(len(doc["claims"]), 1)
        self.assertEqual(doc["claims"][0]["value"], 58.82)
        self.assertEqual(doc["claims"][0]["ref"], "2#groups[0].rate")
        self.assertIn("夜班vs白班", doc["advice"][0], "被引号包住的那个词不能丢")

    def test_clean_json_is_not_marked_repaired(self):
        # 反向断言：结构正常的不许进修复通道，否则「修过」这个标记就没意义了
        doc = nodes._parse_claims('{"conclusion": "x", "claims": [], "advice": ["正常"]}')
        self.assertIsNotNone(doc)
        self.assertFalse(doc.get("_repaired"))

    def test_half_pair_is_not_guessed(self):
        # 只有一半引号时不敢猜它想包到哪，宁可判失败也不瞎修
        self.assertIsNone(nodes._parse_claims(
            '{"conclusion": "x", "claims": [], "advice": ["只开了一个"引号"]}'))

    def test_repair_does_not_bypass_checking(self):
        # 最要紧的一条：修完引号，ref 该判错还是判错。
        # 否则「修复」就成了给校验层留的后门。
        bad = self.BAD.replace('"2#groups[0].rate"', '"1#no_such_field"')
        st = self._state()
        st["final"] = bad
        st["tool_names"] = ["query_defect_log"]
        st["steps"] = 3
        st["user_msg"] = ""
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("rep", model="m", provider="p", base_url="b"):
                res = nodes.verify(st)
        finally:
            trace.RUNS_DIR = old
        cs = res["cite_stats"]
        self.assertEqual(cs["json_bad"], 0, "格式已经修好了，不该再判 json_bad")
        self.assertEqual(cs["json_fixed"], 1)
        self.assertEqual(cs["path_miss"] + cs["ref_bad"], 1,
                         "修完照样要对证据，指错字段必须判失败")

    def _state(self):
        return {"turn": 1, "evidence": [
            {"turn": 1, "tool": "query_defect_log",
             "args": {"device_id": "CX-104"},
             "result": json.dumps({"groups": [{"rate": 0.15}]}), "truncated": False}]}


class ResultEventContractTest(unittest.TestCase):
    """result 事件的两个出口（CLI/评测 与 Web 流式）必须写同样的字段。

    分头写过一次：server._sse 只写 final_head，不写 final 全文。
    同一次运行，走 CLI 能离线复算「这条结论为什么被/没被拦」，走 Web 不能——
    而且不会有任何报错，只有在想回溯线上问题时才发现证据不全，
    那时重跑的结果已经不一定一样了。
    """

    REQUIRED = {"turn", "intent", "steps", "stop_reason", "check_rounds",
                "tools", "blocked", "misquoted",
                "device_ids", "slots", "final", "final_head", "final_text", "cite"}

    def test_all_audit_fields_present(self):
        st = {"turn": 2, "intent": "diagnose", "steps": 5, "tool_names": ["a"],
              "blocked": [], "misquoted": ["伪引用一处"],
              "device_ids": ["CX-104"], "slots": {"device_id": "CX-104"},
              "final": '{"conclusion": "x", "claims": [], "advice": []}',
              "final_text": "x", "cite_stats": {"qty": 1, "cited_ok": 1}}
        ev = build.result_event(st)
        missing = self.REQUIRED - set(ev)
        self.assertFalse(missing, f"result 事件缺字段：{sorted(missing)}")
        # final 必须是全文而不是 head，否则离线复算拿到的是另半道题
        self.assertEqual(ev["final"], st["final"])
        self.assertLessEqual(len(ev["final_head"]), 300)

    def test_no_handwritten_result_event(self):
        # 源码级约束：每一处写 result 事件的地方都必须带 **result_event(...)。
        # 只在字段层面测，测不出「有人又手写一份」——而手写正是这次退化的成因。
        app_dir = Path(__file__).resolve().parents[1] / "app"
        hits = []
        for py in app_dir.rglob("*.py"):
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if 'trace.emit("result"' in line or 't.write("result"' in line:
                    hits.append((py.name, i, line.strip()))
        self.assertTrue(hits, "没扫到任何 result 事件写法，断言条件已失效，需要跟着改")
        for name, line_no, line in hits:
            self.assertIn("result_event", line,
                          f"{name}:{line_no} 又手写了一份 result 字段：{line}")


class StopReasonTest(unittest.TestCase):
    """结束原因必须能把三件事分开：还要查 / 够了 / 被预算掐断。

    改之前"这次运行结束了"有三条来路（步数满 12、模型不再调工具、plan_retry 退一次），
    全部汇进同一个出口，报告上长得一模一样：一次 12 步仍没结论、被预算掐断的运行，
    和一次 3 步数据够了、干净收工，都是 OK / 步数 12 / 语义 1/1，
    但两者可信度差着量级，事后也没法归因。

    取值对齐 Anthropic Messages API 的 stop_reason：
    need_more~tool_use、enough~end_turn、budget_exhausted~max_tokens。
    关键一条：budget_exhausted 只在"还想继续 + 额度已尽"时成立，
    不是"步数满"就成立——这正是 Anthropic 把 end_turn 和 max_tokens
    分成两个值的理由：一个是被截断，一个是被完成。
    """

    def test_budget_exhausted_only_when_still_wanting_more(self):
        self.assertEqual(nodes._classify_stop(True, 12), nodes.STOP_BUDGET_EXHAUSTED)
        # 反向断言：步数满、但模型自己说够了，仍然是正常收工。
        # 把两者写成同一个值的实现会正好挂在这一行。
        self.assertEqual(nodes._classify_stop(False, 12), nodes.STOP_ENOUGH)

    def test_under_budget_stays_need_more(self):
        # 边界：11/12 还想查，还剩一次额度，不能提前报预算耗尽
        self.assertEqual(nodes._classify_stop(True, 11), nodes.STOP_NEED_MORE)

    def test_short_run_is_enough(self):
        self.assertEqual(nodes._classify_stop(False, 3), nodes.STOP_ENOUGH)

    def test_route_sends_budget_case_to_finalize(self):
        st = {"stop_reason": nodes.STOP_BUDGET_EXHAUSTED,
              "messages": [{"role": "assistant", "content": "",
                            "tool_calls": [{"id": "1"}]}],
              "steps": 12}
        self.assertEqual(nodes.route_after_plan(st), "finalize")

    def test_route_sends_enough_to_check(self):
        # 判断够了不再直冲 verify：先进 check 自查，
        # 否则模型抄错的数只能等到终点才暴露，那会儿已经没有改的机会了
        st = {"stop_reason": nodes.STOP_ENOUGH,
              "messages": [{"role": "assistant", "content": '{"conclusion": "x"}'}],
              "steps": 3}
        self.assertEqual(nodes.route_after_plan(st), "check")

    def test_missing_stop_reason_falls_back_to_same_rule(self):
        # 多轮会话从旧 checkpoint 恢复时，state 里还没有这个字段。
        # 缺字段就静默走错分支，是这类改动最容易留下的坑。
        st = {"messages": [{"role": "assistant", "content": "",
                            "tool_calls": [{"id": "1"}]}],
              "steps": 12}
        self.assertEqual(nodes.route_after_plan(st), "finalize")

    def test_intake_clears_stop_reason_between_turns(self):
        # lookup / knowledge 根本不跑 plan，不清就会把上一轮诊断留下的
        # 结束原因当成本轮结论报出去。
        out = nodes.intake({"user_msg": "CX-104 参数是多少", "turn": 1,
                            "stop_reason": nodes.STOP_BUDGET_EXHAUSTED})
        self.assertEqual(out.get("stop_reason"), "")

    def test_missing_reaches_plan_notes(self):
        # 这条是本轮的实质修复：understand 抽出的缺口，plan 必须真的送给模型。
        # 改之前 missing 写了没人读，只在代码里"存在"，不在决策里生效。
        notes, gaps, _ = nodes._plan_notes(
            {"missing": ["该工位的历史标定记录", "光源已用时长"]}, 4)
        joined = "\n".join(notes)
        self.assertIn("该工位的历史标定记录", joined)
        self.assertIn("光源已用时长", joined)
        self.assertEqual(gaps, ["该工位的历史标定记录", "光源已用时长"])

    def test_empty_missing_adds_no_blank_section(self):
        # 没缺口时不能塞一个空标题——那会让模型以为"缺口是空的"，
        # 而事实是"这一轮没判断出缺口"，两者不是一回事。
        notes, gaps, _ = nodes._plan_notes({"missing": []}, 4)
        self.assertEqual(gaps, [])
        self.assertFalse(any("还缺的信息" in n for n in notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
