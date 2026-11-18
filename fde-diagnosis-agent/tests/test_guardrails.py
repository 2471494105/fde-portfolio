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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import trace
from app.graph import build, nodes
from app.graph import state as graph_state
from app.tools import diagnosis, manual, workorder
from eval import run_eval
from mock_qms import server


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
        # tool content 现在是 "[证据 N] {json}"，不能直接 json.loads。
        # 走 _load_obj —— 那是生产路径上唯一解析证据的地方，测试跟着走同一条路，
        # 才不会出现"测试解析方式比生产宽松"的假绿。
        res = nodes._load_obj({"result": out["messages"][0]["content"]})
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


class IdemKeyTest(unittest.TestCase):
    """幂等键的稳定性：同一件还没处理完的事，必须永远算出同一个键。

    这条测的是"跨天重复建单"的根。旧实现把 day 塞进 hash，于是同一件
    还没办完的事，第二天算出来就是一个新键，再建一张单。现场表现是
    CX-101 一个坏光源连着三天三张"待处理"，设备科不知道该处理哪张。
    """

    def test_signature_has_no_date(self):
        """反向断言：键函数的参数里不能出现任何日期维度。

        有人为了"每天允许建一张"再把 day 加回去，这条直接挂。
        """
        import inspect
        params = list(inspect.signature(server._idem_key).parameters)
        self.assertEqual(params, ["device_id", "root_cause_code"],
                         "幂等键不能依赖日期：工单的生命周期不是一天")

    def test_same_device_same_code_is_stable(self):
        a = server._idem_key("CX-101", "light_degradation")
        b = server._idem_key("CX-101", "light_degradation")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_code_changes_key(self):
        """不同故障不能互相吞掉：光源老化和标定漂移是两件事。"""
        self.assertNotEqual(server._idem_key("CX-101", "light_degradation"),
                            server._idem_key("CX-101", "calibration_drift"))

    def test_device_changes_key(self):
        self.assertNotEqual(server._idem_key("CX-101", "light_degradation"),
                            server._idem_key("CX-102", "light_degradation"))


class LightLifeFieldsTest(unittest.TestCase):
    """光源寿命字段：两个方向各一个字段，都不出负数。

    测的是"字段名和值的方向必须一致"。旧实现只有一个 over_life_hours，
    没超寿命时是 -1585，而文案写成"寿命正常（剩余 1585 小时）"——
    同一件事两种符号。模型照文案说 1585、ref 指字段里的 -1585，
    判据一比就判"引用写错"，而错在数据结构上，改 prompt 治不好。
    """

    def test_over_life_is_non_negative(self):
        for cum, rated in ((8620, 8000), (6415, 8000), (3960, 8000), (8000, 8000)):
            over, remaining, _ = diagnosis._life_fields(cum, rated)
            self.assertGreaterEqual(over, 0, f"{cum}/{rated} 的 over_life_hours 是负数")
            self.assertGreaterEqual(remaining, 0, f"{cum}/{rated} 的 remaining_hours 是负数")

    def test_two_directions_are_mutually_exclusive(self):
        """一个有值，另一个必须是 0。同时非零说明换算写错了方向。"""
        for cum, rated in ((8620, 8000), (6415, 8000), (8000, 8000)):
            over, remaining, _ = diagnosis._life_fields(cum, rated)
            self.assertFalse(over > 0 and remaining > 0, "两个方向同时非零")

    def test_over_life_when_exceeded(self):
        over, remaining, _ = diagnosis._life_fields(8620, 8000)
        self.assertEqual(over, 620)
        self.assertEqual(remaining, 0)

    def test_remaining_when_under_life(self):
        over, remaining, _ = diagnosis._life_fields(6415, 8000)
        self.assertEqual(over, 0)
        self.assertEqual(remaining, 1585)

    def test_estimate_does_not_repeat_the_numbers(self):
        """反向断言：定性文案里不能再出现同一份数字。

        有人为了"让模型看得懂"把 620 / 1585 写回文案，这条直接挂——
        那就又变成两个数据源，模型抄哪一个都可能和 ref 对不上。
        唯一允许的数字是"误判率上升 10%-25%"这类知识性区间，它不是寿命值。
        """
        for cum, rated, banned in ((8620, 8000, "620"), (6415, 8000, "1585")):
            _, _, estimate = diagnosis._life_fields(cum, rated)
            self.assertNotIn(banned, estimate,
                             f"文案复述了寿命数字 {banned}，又造出第二个数据源")


class MultiValueClaimTest(unittest.TestCase):
    """多值 claim：一条 claim 里装 N 个「标识 + 数值」对。

    H1/H3 的形状：三个日期三个亮度值、四次标定四个漂移值，塞进一条 claim，
    ref 指整个数组。旧结构只能装一个 value，模型只好把值写在文本里、
    把数组当 ref，判据拿 list 跟 float 比一律失败——可模型并不总是编数，
    它只是没有地方放"这个值属于哪条记录"。

    这一类的分界线是：**值在数组里出现过** vs **这个值属于这条记录**。
    H1 里 85/80/65 三个数都在数组里，但把 85 配到 09-15 就是没在读数据。
    """

    BRIGHT = ('{"brightness_history": ['
              '{"ts": "2025-03-12 09:00:00", "value": 85},'
              '{"ts": "2026-09-15 14:20:00", "value": 80},'
              '{"ts": "2026-10-20 10:05:00", "value": 65}]}')

    CALIB = ('{"records": ['
             '{"ts": "2026-11-02 11:00:00", "drift_px": 0.369, "drift_mm": 0.064},'
             '{"ts": "2026-10-14 12:00:00", "drift_px": 0.654, "drift_mm": 0.115}]}')

    def _verify(self, doc, payload, tool):
        final = json.dumps(doc, ensure_ascii=False)
        ev = [{"result": payload, "tool": tool, "turn": 1,
               "truncated": False, "ev_no": 1, "sig": None}]
        state = {"final": final, "evidence": ev, "turn": 1,
                 "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("multi", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_items_all_paired_passes(self):
        res = self._verify({"conclusion": "亮度逐次下调", "claims": [
            {"text": "亮度从 85 降到 80 再降到 65",
             "ref": "query_light_usage#brightness_history",
             "items": [{"key": "2025-03-12", "field": "value", "value": 85},
                       {"key": "2026-09-15", "field": "value", "value": 80},
                       {"key": "2026-10-20", "field": "value", "value": 65}]}]},
            self.BRIGHT, "query_light_usage")
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["multi_ok"], 1)

    def test_items_wrong_pairing_fails(self):
        """反向断言：值都在数组里，但配错了日期 → 必须判失败。

        85 确实出现在返回里（2025-03-12 那条），可模型把它挂到 2026-09-15 上，
        那条实际是 80。这正是 H1 的病根：不是编数，是没在读数据。
        只要判据能拦住这一步，"值出现过就算过"的宽松做法就不需要了。
        """
        res = self._verify({"conclusion": "亮度逐次下调", "claims": [
            {"text": "2026-09-15 亮度 85",
             "ref": "query_light_usage#brightness_history",
             "items": [{"key": "2026-09-15", "field": "value", "value": 85}]}]},
            self.BRIGHT, "query_light_usage")
        self.assertGreaterEqual(res["cite_stats"]["multi_bad"], 1)
        self.assertTrue(any("对不上" in m for m in res["misquoted"]))

    def test_items_require_array_ref(self):
        """反向断言：ref 指数值字段时 items 无处可放，必须判失败。

        否则模型可以写 ref=cumulative_hours（一个数）+ items 三条，
        三条都不校验，等于给多值开了一条免检通道。
        """
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "三次亮度",
             "ref": "query_light_usage#cumulative_hours",
             "items": [{"key": "2025-03-12", "value": 85}]}]},
            '{"cumulative_hours": 8620}', "query_light_usage")
        self.assertGreaterEqual(res["cite_stats"]["multi_bad"], 1)
        self.assertTrue(any("要求 ref 指向数组" in m for m in res["misquoted"]))

    def test_items_field_pins_the_column(self):
        """H3 的形状：一条记录里有 drift_px 和 drift_mm 两个数。

        指定 field 就不能串到另一列去——0.064 在 drift_mm 里，
        而 drift_px 也有接近的值，不锁字段就会"错列也对上"。
        """
        res = self._verify({"conclusion": "漂移平稳", "claims": [
            {"text": "11-02 漂移 0.064mm、10-14 漂移 0.115mm",
             "ref": "query_calibration#records",
             "items": [{"key": "2026-11-02", "field": "drift_mm", "value": 0.064},
                       {"key": "2026-10-14", "field": "drift_mm", "value": 0.115}]}]},
            self.CALIB, "query_calibration")
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["multi_ok"], 1)

    # ---- 2026-11-17：全量 27 条暴露的两处判据自伤，各配一条正例 + 一条反向 ----

    def _verify_many(self, doc, payloads, sigs=None):
        """同一工具按不同参数多次调用时的 verify。逐个传 payload 和它对应的指纹。"""
        final = json.dumps(doc, ensure_ascii=False)
        ev = [{"result": p, "tool": "query_defect_log", "turn": 1,
               "truncated": False, "ev_no": i + 1,
               "sig": (sigs[i] if sigs else None)}
              for i, p in enumerate(payloads)]
        state = {"final": final, "evidence": ev, "turn": 1,
                 "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("multi", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_items_percent_matches_decimal_without_unit(self):
        """H2：证据存 rate=0.0476，模型在 items 里写 4.76。

        items 的每个元素没有 unit 字段可放（unit 挂在 claim 上），
        旧判据拿到 unit="" 就不做百分数换算，4.76 硬比 0.0476，差 100 倍，
        于是"11-06 全期最低"这条正确结论被判成引用写错。
        这只认 ×100 一个方向：证据惯用小数、模型惯写百分数。
        """
        res = self._verify_many(
            {"conclusion": "11-06 是统计期内最低点", "claims": [
                {"text": "11-06 当日误判率 4.76%",
                 "ref": "query_defect_log{group_by=day}#groups",
                 "items": [{"key": "2026-11-06", "field": "rate", "value": 4.76}]}]},
            ['{"groups": [{"key": "2026-11-06 00:00:00", "count": 21, "rate": 0.0476},'
             ' {"key": "2026-11-07 00:00:00", "count": 13, "rate": 0.1538}]}'],
            sigs=["group_by=day"])
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["multi_ok"], 1)

    def test_items_percent_does_not_rescue_wrong_number(self):
        """反向断言：换算只救"同一量纲写两种"，救不了数真写错。

        47.6 与 0.4483 也对不上（换算后是 44.83），必须继续判失败，
        否则把百分数归一化就等于给"数量级差一档"开了免检。
        """
        res = self._verify_many(
            {"conclusion": "x", "claims": [
                {"text": "1 号工位 47.6%",
                 "ref": "query_defect_log{group_by=station}#groups",
                 "items": [{"key": "1号工位", "field": "rate", "value": 47.6}]}]},
            ['{"groups": [{"key": "1号工位", "count": 29, "rate": 0.4483}]}'],
            sigs=["group_by=station"])
        self.assertGreaterEqual(res["cite_stats"]["multi_bad"], 1)

    def test_items_scan_all_candidate_arrays(self):
        """A3：同一工具按 station / batch 各调一次，模型引的是 batch 那次的数组。

        指纹只写了 group_by=batch，实际指纹还带着 device_id 和 time_from。
        旧判据整串精确相等 → 收窄不到任何一条 → 退回全部候选 →
        next() 取第一个数组（station）→ 拿工位表去查批次号，
        11 条 pairing 全部"对不上"，报出来却是"模型编数"。
        模型引的是"那次返回里的这条记录"，判据分不清是哪次属于判据的缺陷。
        """
        res = self._verify_many(
            {"conclusion": "批次过杀率无单一异常集中", "claims": [
                {"text": "B20261107 批次过杀率 20%",
                 "ref": "query_defect_log{group_by=batch,time_from=2026-09-01}#groups",
                 "items": [{"key": "B20261107", "field": "rate", "value": 0.2}]}]},
            ['{"groups": [{"key": "2号工位", "count": 29, "rate": 0.3103},'
             ' {"key": "1号工位", "count": 27, "rate": 0.1481}]}',
             '{"groups": [{"key": "B20261105", "count": 81, "rate": 0.5062},'
             ' {"key": "B20261107", "count": 15, "rate": 0.2}]}'],
            sigs=["group_by=station",
                  "device_id=CX-102,group_by=batch,time_from=2026-09-01"])
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["multi_ok"], 1)

    def test_items_still_fail_when_no_candidate_array_matches(self):
        """反向断言：逐数组尝试不是"总有一个放过"。

        两个候选数组都配不上这条记录时仍然判失败——放宽的只是"指哪一次"，
        不是"这个值到底在不在"。
        """
        res = self._verify_many(
            {"conclusion": "x", "claims": [
                {"text": "B20261107 批次过杀率 90%",
                 "ref": "query_defect_log{group_by=batch}#groups",
                 "items": [{"key": "B20261107", "field": "rate", "value": 0.9}]}]},
            ['{"groups": [{"key": "2号工位", "count": 29, "rate": 0.3103}]}',
             '{"groups": [{"key": "B20261107", "count": 15, "rate": 0.2}]}'],
            sigs=["group_by=station", "group_by=batch"])
        self.assertGreaterEqual(res["cite_stats"]["multi_bad"], 1)


class ParamResolveTest(unittest.TestCase):
    """check_doc_conflict 的参数名归一：唯一候选才自动改名，多候选一律不猜。

    T1 的真相是模型传了「光源亮度」，库里叫「光源亮度设定」，工具只回一句
    "请改用可用参数名重查"，把改名重查的负担全压给模型——它没重查，
    还把"我参数写错了"报成"手册里没有这个数"，归因错了两层。

    归一化能救这一条，但风险也在这里：一旦"差不多的都算一个"，
    模型传「阈值」就会被悄悄替换成「判定置信度阈值」或「日误判率上报阈值」，
    错得比不归一化更隐蔽——调用方看不到程序替他猜了一次。
    """

    KEYS = ["传送带速度(m/s)", "光源亮度设定", "判定置信度阈值",
            "日误判率上报阈值(%)", "相机曝光(us)", "标定周期(天)"]

    def test_exact_name_unchanged(self):
        chosen, status, _ = manual._resolve_param("光源亮度设定", self.KEYS)
        self.assertEqual(chosen, "光源亮度设定")
        self.assertEqual(status, "exact")

    def test_partial_name_resolves_when_unique(self):
        """T1 的真实形状：少个后缀，库里只有一个对得上。"""
        chosen, status, _ = manual._resolve_param("光源亮度", self.KEYS)
        self.assertEqual(chosen, "光源亮度设定")
        self.assertEqual(status, "normalized")

    def test_whitespace_and_fullwidth_are_format_only(self):
        """归一化只抹格式：空格、全角括号。抹到语义就危险了。"""
        self.assertEqual(manual._norm_param(" 光源亮度（%） "), "光源亮度")
        chosen, status, _ = manual._resolve_param("  衰减  设定  ", self.KEYS)
        self.assertEqual(status, "missing", "抹空白不该把不相干的词变成候选")

    def test_multiple_candidates_never_auto_pick(self):
        """反向断言：两个候选时不许替模型做决定。

        「阈值」能对上「判定置信度阈值」和「日误判率上报阈值」，
        自动选一个就等于程序在猜，而且调用方无从知道猜了什么。
        """
        chosen, status, cands = manual._resolve_param("阈值", self.KEYS)
        self.assertEqual(status, "ambiguous")
        self.assertEqual(chosen, "阈值", "没命中就该按原名查，不要偷偷替换")
        self.assertIn("判定置信度阈值", cands)
        self.assertIn("日误判率上报阈值(%)", cands)

    def test_unknown_name_is_missing_not_fuzzy(self):
        """反向断言：不像的就别硬凑。

        模糊匹配没有下限时，任何输入都能"归一化"到某个参数上，
        于是"查错了"永远报不出来。
        """
        chosen, status, cands = manual._resolve_param("不存在的参数", self.KEYS)
        self.assertEqual(status, "missing")
        self.assertEqual(chosen, "不存在的参数")
        self.assertEqual(cands, [])

    def test_extra_qualifier_also_resolves(self):
        """模型多带限定词（"产线光源亮度设定"）也要能吃下——包含关系是双向看的。"""
        chosen, status, _ = manual._resolve_param("产线光源亮度设定", self.KEYS)
        self.assertEqual(chosen, "光源亮度设定")
        self.assertEqual(status, "normalized")


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

    def _verify(self, doc, *payloads, truncated=False, tool="query_defect_log",
                sigs=None):
        final = doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
        ev = [{"result": p, "tool": tool, "turn": 1,
               "truncated": truncated, "ev_no": i + 1,
               "sig": (sigs[i] if sigs else None)}
              for i, p in enumerate(payloads)]
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
             "ref": "query_defect_log#groups[0].rate"}]}, self.GROUPS)
        self.assertEqual(res["blocked"], [])
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["cited_ok"], 1)
        # 小数 ↔ 百分比的换算是这里最容易错的一环，单独断言
        self.assertGreaterEqual(res["cite_stats"]["path_ok"], 1)

    BRIGHT = ('{"brightness_history": [{"ts": "2024-11-08 09:00:00", "value": 78}],'
              ' "brightness_set": 78}')

    def test_single_value_pointing_at_array_asks_for_items(self):
        """ref 指数组、claim 是单值 → 要说"结构装不下"，不许说成"值不存在"。

        A4 的原话是「候选值 [{'ts': '2024-11-08 09:00:00', 'value': 78}] 都不是 78」。
        读数的人得自己反推出"哦这是数组引用"——判据明明知道真相（ref 是数组、
        claim 是单值），却把它说成"你编了个数"。
        """
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "亮度设定自装机以来一直是 78", "value": 78,
             "ref": "query_light_usage#brightness_history"}]},
            self.BRIGHT, tool="query_light_usage")
        self.assertEqual(res["cite_stats"]["path_bad"], 1, "判定不变：仍然是失败")
        msg = res["misquoted"][0]
        self.assertIn("items", msg)
        self.assertNotIn("都不是 78", msg)

    def test_single_value_on_scalar_ref_keeps_old_wording(self):
        """反向断言：同一个数、同一个工具，指到标量字段时仍按"值不对"报。

        改话术不能把"值真对不上"也顺带说成"结构装不下"——那是换了个说法骗自己。
        """
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "亮度设定 80", "value": 80,
             "ref": "query_light_usage#brightness_set"}]},
            self.BRIGHT, tool="query_light_usage")
        self.assertEqual(res["cite_stats"]["path_bad"], 1)
        self.assertIn("都不是 80", res["misquoted"][0])

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
             "ref": "query_defect_log#groups[0].cnt"}]}, self.GROUPS)
        self.assertTrue(any("34 条" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["path_miss"], 1)

    def test_value_mismatch_is_misquoted(self):
        # 字段存在但值不对：这是"写出处但写错"，比没写出处严重
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "判定 99 条", "value": 99, "unit": "条",
             "ref": "query_defect_log#groups[0].count"}]}, self.GROUPS)
        self.assertTrue(any("99 条" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["path_bad"], 1)

    def test_qualitative_claim_with_dead_ref_is_caught(self):
        """定性 claim 没有数字，也必须有能指得着的 ref。

        现状是 v 为空且无 items/quote 时直接 continue——连 ref 都不看。
        模型写 `{"text": "1 号工位是主因", "ref": "query_defect_log#groups[99]"}`，
        判据一句话都不说。可"这句话针对哪条记录"在没有数字时反而更要紧：
        没有值可以核，ref 就是唯一凭据。
        """
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "1 号工位是主要问题工位",
             "ref": "query_defect_log#groups[99]"}]}, self.GROUPS)
        self.assertEqual(res["cite_stats"]["qual_bad"], 1, "ref 指不到要单独记失败")
        self.assertEqual(res["cite_stats"]["path_miss"], 0,
                         "不能混进数值路径缺失的计数，两类修法不同")
        self.assertTrue(any("groups[99]" in m for m in res["misquoted"]))

    def test_qualitative_claim_with_live_ref_passes(self):
        """反向：ref 指得到就算站住，不比较值——本来就没有值可比。"""
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "1 号工位是主要问题工位",
             "ref": "query_defect_log#groups[0]"}]}, self.GROUPS)
        self.assertEqual(res["cite_stats"]["qual_ok"], 1)
        self.assertEqual(res["misquoted"], [])
        self.assertEqual(res["cite_stats"]["qty"], 0, "定性不是数字，不进数字分母")

    def test_qualitative_claim_without_ref_is_grey(self):
        """定性 claim 没写出处 → 标灰，不判死。

        它和"有数字没出处"同性质：都提供了无法核验的断言，但都没说假话。
        判死会让纯推理型 claim（"因此判断为光源老化"）没法写，
        而这类推理本来就该留在 claims 里接受审视。
        """
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "因此判断为光源老化"}]}, self.GROUPS)
        self.assertEqual(len(res["blocked"]), 1)
        self.assertEqual(res["misquoted"], [])
        self.assertIn("[无出处]", res["final_text"])

    def test_ref_to_text_field_matches_number_inside(self):
        # 工具返回的评估说明是整段文字（"上升 10%-25%"），模型引用这段里的 25。
        # 字段不是数字，但里面有这个数——不能因为类型不是 float 就判成引用写错。
        res = self._verify({"conclusion": "光源超寿命", "claims": [
            {"text": "误判率通常上升 10%~25%", "value": 25, "unit": "%",
             "ref": "query_light_usage#assessment"}]},
            '{"assessment": "已超额定寿命 620 小时，上升 10%-25%，建议更换光源"}',
            tool="query_light_usage")
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["path_ok"], 1)

    def test_unknown_tool_name_is_ref_bad(self):
        # 写了一个仓库里根本不存在的工具名：这是编的出处，不是"被裁剪"。
        # 两者必须分开——前者该判失败，后者该标灰。
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "误判率 58.82%", "value": 58.82, "unit": "%",
             "ref": "query_magic_box#groups[0].rate"}]}, self.GROUPS)
        self.assertGreaterEqual(res["cite_stats"]["ref_bad"], 1)
        self.assertEqual(res["cite_stats"]["cite_oob"], 0)
        self.assertTrue(any("不存在的工具名" in m for m in res["misquoted"]))

    def test_numbered_ref_is_rejected_not_reinterpreted(self):
        # 档 C 之后不再有编号。写 "1#..." 必须判格式错，
        # 不能悄悄当成"第 1 条"放过——那等于两套引用语义并存。
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "误判率 58.82%", "value": 58.82, "unit": "%",
             "ref": "1#groups[0].rate"}]}, self.GROUPS)
        self.assertGreaterEqual(res["cite_stats"]["ref_bad"], 1)
        self.assertEqual(res["cite_stats"]["cited_ok"], 0)
        self.assertTrue(any("工具名#字段路径" in m for m in res["misquoted"]))

    def test_multi_call_without_fingerprint_reports_ambiguity(self):
        """同一工具按两种参数各调一次、ref 又没写指纹 → 报"歧义"，不报"值不符"。

        A2 的真实形状：4 次 query_defect_log，模型说"2号工位 36.36%"，
        ref 只写 groups[0].rate。判据取到 defect_type 那次的 0.6481，
        报出来是"字段值 0.6481 ≠ 36.36%"，读起来像模型算错。
        实际该说的是"你没写指哪一次"——两种修法完全不同（改 prompt vs 补指纹）。
        """
        res = self._verify(
            {"conclusion": "x", "claims": [
                {"text": "2号工位误判率 36.36%", "value": 36.36, "unit": "%",
                 "ref": "query_defect_log#groups[0].rate"}]},
            '{"groups": [{"key": "划痕", "count": 54, "rate": 0.6481}]}',
            '{"groups": [{"key": "2号工位", "count": 32, "rate": 0.3438}]}',
            sigs=["group_by=defect_type", "group_by=station"])
        self.assertGreaterEqual(res["cite_stats"]["ref_ambiguous"], 1)
        self.assertEqual(res["cite_stats"]["path_bad"], 0)
        self.assertTrue(any("引用歧义" in m for m in res["misquoted"]))
        self.assertTrue(any("参数指纹" in m for m in res["misquoted"]))

    def test_multi_call_with_fingerprint_still_reports_value_mismatch(self):
        """反向断言：写了指纹就不该被"歧义"兜住。

        指纹把候选收窄到一组，判据确实知道它指哪次——这时值还对不上，
        就是真编造，必须报"字段值 ≠"而不是"歧义"。否则给模型留了条后路：
        写个命中不了的指纹，就能把编造洗成"指不清"。
        """
        res = self._verify(
            {"conclusion": "x", "claims": [
                {"text": "2号工位误判率 99%", "value": 99, "unit": "%",
                 "ref": "query_defect_log{group_by=station}#groups[0].rate"}]},
            '{"groups": [{"key": "划痕", "count": 54, "rate": 0.6481}]}',
            '{"groups": [{"key": "2号工位", "count": 32, "rate": 0.3438}]}',
            sigs=["group_by=defect_type", "group_by=station"])
        self.assertGreaterEqual(res["cite_stats"]["path_bad"], 1)
        self.assertEqual(res["cite_stats"]["ref_ambiguous"], 0)
        # 报错要同时说清"哪个数"和"候选里没有它"。旧文案只写 `字段值 {hits[0]} ≠ v`，
        # 当候选有多个数组时 hits[0] 可能来自模型根本没引的那一组，读者会当成编数。
        self.assertTrue(any("都不是 99%" in m for m in res["misquoted"]))

    def test_single_call_without_fingerprint_is_not_ambiguous(self):
        """只有一次调用时不存在歧义，仍按值判——别把单次的不符也洗成"指不清"。"""
        res = self._verify(
            {"conclusion": "x", "claims": [
                {"text": "判定 99 条", "value": 99, "unit": "条",
                 "ref": "query_defect_log#groups[0].count"}]},
            '{"groups": [{"key": "划痕", "count": 54, "rate": 0.6481}]}',
            sigs=["group_by=defect_type"])
        self.assertGreaterEqual(res["cite_stats"]["path_bad"], 1)
        self.assertEqual(res["cite_stats"]["ref_ambiguous"], 0)

    def test_no_json_is_failure_not_silent_pass(self):
        # 旧文本解析路径已删。模型不按契约输出时判失败，不再静默退回——
        # 退回等于"这条没校验"，而没校验的结论被当成通过，才是真正危险的地方。
        res = self._verify("标定周期 90 天[query_device_profile#maintenance_cycle_days]。",
                           '{"maintenance_cycle_days": 90}',
                           tool="query_device_profile")
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

    def _verify(self, doc, *payloads, tool="query_light_usage"):
        final = json.dumps(doc, ensure_ascii=False)
        ev = [{"result": p, "tool": tool, "turn": 1,
               "truncated": False, "ev_no": i + 1}
              for i, p in enumerate(payloads)]
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
             "ref": "query_light_usage#assessment"}]}, self.RANGE_TEXT)
        self.assertEqual(res["misquoted"], [])
        self.assertGreaterEqual(res["cite_stats"]["range_ok"], 1)
        self.assertEqual(res["cite_stats"]["range_bad"], 0)

    def test_range_on_numeric_field_is_misquoted(self):
        # 反向断言：ref 指着一个精确数值（0.1541 = 15.41%），
        # 却用它担保 10%~25%——那个上界不是这个位置给的。必须判失败。
        # 只要实现里少了"数值字段禁 value_max"这条，这一行就会挂。
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "误判率 10%~25%", "value": 10, "value_max": 25, "unit": "%",
             "ref": "query_light_usage#rate"}]}, self.RANGE_TEXT)
        self.assertTrue(any("撑不住区间" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["range_bad"], 1)

    def test_range_missing_one_endpoint_is_misquoted(self):
        # 只找到一端不算：另一端是模型自己加的，不是原文里的
        res = self._verify({"conclusion": "x", "claims": [
            {"text": "上升 10%~40%", "value": 10, "value_max": 40, "unit": "%",
             "ref": "query_light_usage#assessment"}]}, self.RANGE_TEXT)
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
             "ref": "query_light_usage#assessment"}]}, self.RANGE_TEXT)
        self.assertTrue(any("上下界写反" in m for m in res["misquoted"]))
        self.assertGreaterEqual(res["cite_stats"]["range_bad"], 1)


class _FakeResp:
    """假 completion 响应：understand 只读 choices[0].message.content 和 usage。"""

    def __init__(self, content):
        msg = type("M", (), {"content": content})
        self.choices = [type("C", (), {"message": msg})]
        self.usage = None


class _FakeCompletions:
    def __init__(self, content, raises=None):
        self.content = content
        self.raises = raises

    def create(self, **kw):
        if self.raises:
            raise self.raises
        return _FakeResp(self.content)


class _FakeClient:
    def __init__(self, content, raises=None):
        self.chat = self
        self.completions = _FakeCompletions(content, raises)


class IntentConfidenceTest(unittest.TestCase):
    """意图判断的自报置信度闸。

    融合 Jev 的核心做法：分类器的价值不只在选得准，还在**能报出"我不确定"**。
    原来这一步是 temperature=0 的五选一，没有置信度——判错时下游无从知道它有多虚，
    只能整条路径跑到底。

    两条线：低于 CONF_ASK 降级追问；介于两线之间照走但带免责说明。
    必须能区分"没给置信度"（旧行为，不拦）与"给了低置信度"（要拦）。
    """

    def _run(self, payload, user_msg="cx-104 数据", device_id="CX-104"):
        content = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
        client = _FakeClient(content)
        st = {"user_msg": user_msg, "device_id": device_id, "turn": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(nodes, "get_client", return_value=client):
                with trace.Tracer("conf", model="m", provider="p", base_url="b"):
                    return nodes.understand(st)
        finally:
            trace.RUNS_DIR = old

    def test_low_confidence_falls_back_to_ask(self):
        # 带着错的理解跑满 8 步，不如先问一句
        out = self._run({"intent": "diagnose", "confidence": 0.45,
                         "device_ids": ["CX-104"], "phenomenon": "误判率高"})
        self.assertEqual(out["intent"], "clarify")
        self.assertIn("低于", out["intent_note"])

    def test_mid_confidence_keeps_intent_with_note(self):
        # 落在两条线之间：照判断走，但结论里要带"这是按 X 理解的"
        out = self._run({"intent": "lookup", "confidence": 0.70,
                         "device_ids": ["CX-104"]})
        self.assertEqual(out["intent"], "lookup")
        self.assertIn("按「lookup」理解", out["intent_note"])

    def test_note_never_disagrees_with_final_intent(self):
        # 硬矛盾已经把 intent 降成 clarify 时，不能再挂"按 diagnose 理解"的说明——
        # 用户看到的是"你说按诊断理解，那你为什么还问我"。
        out = self._run({"intent": "diagnose", "confidence": 0.82,
                         "device_ids": [], "phenomenon": "夜班更差"}, device_id="")
        self.assertEqual(out["intent"], "clarify")
        self.assertEqual(out["intent_note"], "")

    def test_high_confidence_no_note(self):
        out = self._run({"intent": "lookup", "confidence": 0.95,
                         "device_ids": ["CX-104"]})
        self.assertEqual(out["intent"], "lookup")
        self.assertEqual(out["intent_note"], "")

    def test_missing_confidence_keeps_old_behaviour(self):
        # 模型没给这个字段时不能一刀切成追问——那会把所有结论都降级
        out = self._run({"intent": "lookup", "device_ids": ["CX-104"]})
        self.assertEqual(out["intent"], "lookup")
        self.assertEqual(out["intent_conf"], 1.0)

    def test_string_confidence_is_parsed(self):
        # 模型常把数字写成字符串，不能因此当成"没给"
        out = self._run({"intent": "lookup", "confidence": "0.40",
                         "device_ids": ["CX-104"]})
        self.assertEqual(out["intent"], "clarify")

    def test_llm_failure_falls_back_to_cheapest_path(self):
        # 有设备号时兜底原来跑 diagnose（最贵的一条）。改成 lookup。
        client = _FakeClient("", raises=RuntimeError("boom"))
        st = {"user_msg": "cx-104 数据", "device_id": "CX-104", "turn": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(nodes, "get_client", return_value=client):
                with trace.Tracer("conf", model="m", provider="p", base_url="b"):
                    out = nodes.understand(st)
        finally:
            trace.RUNS_DIR = old
        self.assertEqual(out["intent"], "lookup")


class AskUserWordingTest(unittest.TestCase):
    """追问文案：已经有的信息不能再让用户说一遍。

    用户最烦的两句是"你刚才说的我没听见"和"我刚说过的你再问一次"。
    这两个各有一个实测样本：说了「误判率有点高」却回「需要两个信息」；
    上一轮确认过 CX-104，这一轮说「就是那个工位」又被要一次设备号。
    """

    def test_phenomenon_known_only_asks_for_device(self):
        out = nodes.ask_user({"device_ids": [], "phenomenon": "误判率有点高",
                              "slots": {}, "missing": []})
        # 读 final_text（给人看的那份），不读 final：
        # final 是结构化结论（dict），出到人眼前才序列化。
        # 测试若耦合"final 一定是字符串"，改成 dict 时会在四条无关用例上误报。
        txt = out["final_text"]
        self.assertIn("误判率有点高", txt)
        self.assertIn("CX-101 ~ CX-105", txt)
        self.assertNotIn("需要两个信息", txt)

    def test_confirmed_device_in_slots_is_not_asked_again(self):
        # 本轮没提设备、上一轮确认过 → 文案直接用，不再索要
        out = nodes.ask_user({"device_ids": [], "phenomenon": "夜班更差",
                              "slots": {"device_id": "CX-104"}, "missing": []})
        txt = out["final_text"]
        self.assertIn("CX-104", txt)
        self.assertIn("夜班更差", txt)
        self.assertNotIn("还缺一个设备编号", txt)

    def test_nothing_known_asks_for_both(self):
        out = nodes.ask_user({"device_ids": [], "phenomenon": "",
                              "slots": {}, "missing": []})
        self.assertIn("需要两个信息", out["final_text"])

    def test_intent_note_is_prefixed(self):
        # 低置信降级时先告诉用户"我是按哪种理解在问"
        out = nodes.ask_user({"device_ids": ["CX-104"], "phenomenon": "",
                              "slots": {}, "missing": [],
                              "intent_note": "意图置信度 0.45 低于 0.6，先追问确认再往下走"})
        self.assertTrue(out["final_text"].startswith("意图置信度 0.45"))

    def test_ask_user_gives_readable_text_for_frontend(self):
        # 追问是唯一一条"不经过 verify"的路径，所以它必须自己带上 final_text：
        # 前端读 `final_text || final`，只给结构化 final 会让这一屏显示一段 JSON。
        out = nodes.ask_user({"device_ids": ["CX-104"], "phenomenon": "",
                              "slots": {}, "missing": []})
        self.assertTrue(out["final_text"].strip(), "ask_user 必须给 final_text")
        self.assertIsInstance(out["final"], dict, "final 应走统一结论结构")
        self.assertEqual(out["final"]["claims"], [], "追问里没有可引用的事实")


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
              "evidence": [{"result": self.PAYLOAD, "tool": "query_light_usage", "turn": 1, "ev_no": 1,
                            "truncated": False}],
              "messages": []}
        return nodes.check(st)

    def test_clean_claim_passes_without_retry(self):
        out = self._run_check(json.dumps({"conclusion": "x", "claims": [
            {"text": "2 号工位误判率 11.39%", "value": 11.39, "unit": "%",
             "ref": "query_light_usage#rate"}]}, ensure_ascii=False))
        self.assertEqual(out["check_action"], "pass")
        self.assertEqual(out["check_retry"], 0)
        self.assertNotIn("messages", out)

    def test_bad_claim_retries_once_with_reason(self):
        out = self._run_check(json.dumps({"conclusion": "x", "claims": [
            {"text": "2 号工位误判率 4.84%", "value": 4.84, "unit": "%",
             "ref": "query_light_usage#rate"}]}, ensure_ascii=False))
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
             "ref": "query_light_usage#rate"}]}, ensure_ascii=False), retry=1)
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
             "ref": "query_light_usage#rate"}]}, ensure_ascii=False),
              "evidence": [{"result": self.PAYLOAD, "tool": "query_light_usage", "turn": 1, "ev_no": 1,
                            "truncated": False}],
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
            {"text": "判定 34 条", "value": 34, "unit": "条", "ref": "query_defect_log#groups[0].count"}]}
        final = json.dumps(doc, ensure_ascii=False)
        state = {"final": final, "evidence": [{"result": ev_text, "tool": "query_defect_log",
                                               "turn": 1, "truncated": True, "ev_no": 1}],
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
             "ref": "query_defect_log#groups[0].cnt"}]}, ClaimContractTest.GROUPS, truncated=False)
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
                evidence.append({"result": f'{{"turn": {t}, "i": {j}}}',
                                 "turn": t, "ev_no": len(evidence) + 1})
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

    def test_check_sees_same_evidence_as_verify(self):
        # check（回环前）与 verify（终局）必须看同一批证据。
        # 两边各取一份的后果：check 拿全量，能查到模型早读不到的旧证据；
        # verify 只拿窗口内的，同一条判"不在可见范围"→ 标灰，引用率悄悄少算。
        # 单轮下全量等于窗口，看不出差别；一到多轮就变成"回环都白跑了，问题还在"。
        # 这里第 3 轮的 lo=2，证据 1 已出窗口，所以两边都应只剩 1 条。
        st = {
            "turn": 3,
            "messages": [{"role": "assistant", "content": ""}],
            "evidence": [
                {"result": '{"cnt": 7}', "turn": 1, "ev_no": 1},
                {"result": '{"cnt": 5}', "turn": 2, "ev_no": 2},
            ],
            "final": json.dumps({"conclusion": "x", "claims": [], "advice": []},
                                ensure_ascii=False),
            "user_msg": "", "tool_names": [], "steps": 0,
            "check_retry": 0, "check_notes": [], "check_unsourced0": 0,
        }
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("chk", model="m", provider="p", base_url="b"):
                v = nodes.verify(st)
        finally:
            trace.RUNS_DIR = old

        self.assertEqual(nodes.check(st)["check_ev"], v["cite_stats"]["ev"],
                         "check 与 verify 参与校验的证据条数不同："
                         "check 会放行一条模型读不到的旧证据，"
                         "verify 再把它算成不在可见范围")
        self.assertEqual(v["cite_stats"]["ev"], 1)   # 反向：不能是"一律看全量"

    def test_check_ev_mismatch_is_recorded_not_blocked(self):
        # check_ev 以前只写不读：注释里写着"两边取的条数一旦不同，
        # check 放行、verify 判死"，代码里没人比对过。现在 verify 比了。
        # 不一致是程序口径自相矛盾，不是模型编造 → 记一个数，不判失败。
        def run(check_ev):
            st = {
                "turn": 3,
                "messages": [{"role": "assistant", "content": ""}],
                "evidence": [
                    {"result": '{"cnt": 7}', "turn": 1, "ev_no": 1},
                    {"result": '{"cnt": 5}', "turn": 2, "ev_no": 2},
                ],
                "final": json.dumps({"conclusion": "x", "claims": [], "advice": []},
                                    ensure_ascii=False),
                "user_msg": "", "tool_names": [], "steps": 0,
                "check_retry": 0, "check_notes": [], "check_unsourced0": 0,
                "check_ev": check_ev,
            }
            old = trace.RUNS_DIR
            trace.RUNS_DIR = Path(tempfile.mkdtemp())
            try:
                with trace.Tracer("chk", model="m", provider="p", base_url="b"):
                    return nodes.verify(st)
            finally:
                trace.RUNS_DIR = old

        # 窗口内 1 条，check 却报 2 条 → 口径不一致
        bad = run(2)
        self.assertEqual(bad["cite_stats"]["ev_mismatch"], 1)
        self.assertEqual(bad["misquoted"], [], "口径不一致不该判模型失败")

        # 一致
        self.assertEqual(run(1)["cite_stats"]["ev_mismatch"], 0)

        # None = 本轮根本没跑 check（lookup / knowledge 路径），不能误报
        self.assertEqual(run(None)["cite_stats"]["ev_mismatch"], 0)

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
            {"turn": 1, "tool": "query_defect_log", "ev_no": 1,
             "args": {"device_id": "CX-104"},
             "result": json.dumps({"groups": [{"rate": 0.15}]}), "truncated": False}]}


class EvidenceNumberingTest(unittest.TestCase):
    """引用身份是工具名，不是编号（档 C）。

    为什么换形状：编号是**位置**，会随上下文裁剪和轮次漂移。2026-11-17 三轮探针
    同一份代码跑两次，第 3 轮一次 14/14、一次 0/15——错的全是编号：模型记得
    "第 1 轮查过设备档案"，就写 1#，而真实编号是全局累计、第 3 轮从 5 起。
    工具名是**身份**，第几轮调都叫同一个名字。

    没有这几条反向断言，把请求改回"按编号找"也能全绿——
    那正是这次要消灭的行为。
    """

    def _verify(self, doc, ev):
        final = doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
        state = {"final": final, "evidence": ev, "turn": 1,
                 "user_msg": "", "tool_names": [], "steps": 0}
        old = trace.RUNS_DIR
        trace.RUNS_DIR = Path(tempfile.mkdtemp())
        try:
            with trace.Tracer("num", model="m", provider="p", base_url="b"):
                return nodes.verify(state)
        finally:
            trace.RUNS_DIR = old

    def test_payload_carries_tool_name_and_stays_parsable(self):
        text, cut = nodes._payload({"a": 1}, tool="query_calibration")
        self.assertFalse(cut)
        self.assertTrue(text.startswith("[query_calibration] "))
        self.assertEqual(nodes._load_obj({"result": text}), {"a": 1},
                         "剥前缀后必须还是原结构，否则校验层全线 ref_bad")

    def test_no_tool_means_no_prefix(self):
        # 不传工具名就不加前缀：_payload 还有别的调用方，默认行为不能变。
        text, _ = nodes._payload({"a": 1})
        self.assertEqual(text, '{"a": 1}')

    def test_tool_prefixed_payload_still_respects_limit(self):
        # 前缀的字节要从预算里扣，不能靠 limit 之外白送一节。
        text, cut = nodes._payload({f"k{i}": i for i in range(800)},
                                   limit=200, tool="query_defect_log")
        self.assertTrue(cut)
        self.assertLessEqual(len(text), 200)
        self.assertTrue(text.startswith("[query_defect_log] "))

    def test_ref_matches_by_tool_not_by_position(self):
        # ev 列表第 1 项的工具是 query_calibration。按工具名引用它必须命中；
        # 而引用"编号 7"必须判格式错——档 C 之后编号已经不是引用格式。
        ev = [
            {"ev_no": 7, "tool": "query_calibration", "truncated": False,
             "result": json.dumps({"records": [{"drift_px": 0.467}]})},
            {"ev_no": 8, "tool": "search_manual", "truncated": False,
             "result": json.dumps({"chunks": [{"text": "标定周期为 90 天"}]})},
        ]
        doc = {"conclusion": "x", "claims": [
            {"text": "漂移 0.467 像素", "value": 0.467,
             "ref": "query_calibration#records[0].drift_px"},
            {"text": "标定周期 90 天", "value": 90, "ref": "7#chunks[0].text"}]}
        res = self._verify(doc, ev)
        self.assertEqual(res["cite_stats"]["cited_ok"], 1,
                         "工具名对上了就该成立，跟它是列表第几项无关")
        self.assertEqual(res["cite_stats"]["ref_bad"], 1,
                         "写编号不再是合法格式")
        self.assertEqual(res["cite_stats"]["path_miss"], 0)

    def test_same_tool_twice_any_return_counts(self):
        # 同一工具一轮里调两次（比如换 query 再搜一遍手册），模型记不住"是第几次"。
        # 它抄的那段原文只可能来自其中一次，所以在该工具的所有返回里找——
        # 任一命中即成立。这条如果不成立，档 C 就等于把"编号问题"换成了"次数问题"。
        ev = [
            {"ev_no": 9, "tool": "search_manual", "truncated": False,
             "result": json.dumps({"chunks": [{"text": "标定周期为 90 天"}],
                                   "query": "标定周期"})},
            {"ev_no": 10, "tool": "search_manual", "truncated": False,
             "result": json.dumps({"chunks": [{"text": "漂移上限为 0.3 毫米"}],
                                   "query": "漂移上限"})},
        ]
        doc = {"conclusion": "x", "claims": [
            # 第二批返回里的字段，ref 只写一次工具名，不指明第几次
            {"text": "漂移上限 0.3 毫米", "value": 0.3,
             "unit": "毫米", "ref": "search_manual#chunks[0].text"}]}
        res = self._verify(doc, ev)
        self.assertEqual(res["misquoted"], [],
                         "同一工具两次调用，任一返回里有这个字段就该成立")

    def test_unknown_tool_is_judged_differently_from_cut(self):
        # 工具名不存在（编的）→ ref_bad 判失败；
        # 工具存在但本轮没有它的返回（被裁掉）→ cite_oob 标灰放行。
        # 两类一旦混在一起，"模型编出处"就会伪装成"上下文裁剪"。
        ev = [{"ev_no": 8, "tool": "query_calibration", "truncated": False,
               "result": json.dumps({"records": [{"drift_px": 0.467}]})}]
        doc = {"conclusion": "x", "claims": [
            {"text": "漂移 0.1", "value": 0.1, "ref": "query_magic_box#drift_px"},
            {"text": "保养周期 90 天", "value": 90,
             "ref": "query_device_profile#maintenance_cycle_days"}]}
        res = self._verify(doc, ev)
        self.assertEqual(res["cite_stats"]["ref_bad"], 1, "编工具名必须判失败")
        self.assertEqual(res["cite_stats"]["cite_oob"], 1,
                         "工具真实存在、只是不在本轮视野 → 标灰")
        self.assertEqual(len(res["blocked"]), 1)

    def test_act_tags_payload_with_tool_name(self):
        # act 写进 messages 的标签必须是工具名：多轮里它是模型唯一能抄的东西，
        # 印错了（比如还印编号）就等于把漂移源又装回去。
        def fake(device_id):
            return {"device_id": device_id, "line": "4号线"}

        state = {
            "messages": [{"role": "assistant", "tool_calls": [{
                "id": "c1",
                "function": {"name": "query_device_profile",
                             "arguments": json.dumps({"device_id": "CX-101"})}}]}],
            "evidence": [{"ev_no": i} for i in range(1, 6)],
            "device_ids": ["CX-101"], "device_id": "CX-101", "turn": 2,
        }
        with mock.patch.dict(nodes.TOOL_IMPLS, {"query_device_profile": fake}):
            out = nodes.act(state)
        # 标签 = 工具名 + 参数指纹。指纹必须出现，否则同一工具两次调用分不出哪次
        self.assertEqual(
            out["messages"][0]["content"].split("] ", 1)[0] + "] ",
            "[query_device_profile{device_id=CX-101}] ")
        # 剥掉前缀仍要能解析回原结构
        self.assertEqual(nodes._load_obj({"result": out["messages"][0]["content"]}),
                         {"device_id": "CX-101", "line": "4号线"})
        # ev_no 仍要按累计数续，它是审计用的，不是引用用的
        self.assertEqual(out["evidence"][0]["ev_no"], 6)
        self.assertEqual(out["evidence"][0]["sig"], "device_id=CX-101")

    def test_payload_carries_arg_fingerprint(self):
        # 指纹取自参数、键排序、跳空值：同一组参数必须永远得到同一个字符串，
        # 校验端要靠它做等值比较。
        text, _ = nodes._payload({"a": 1}, tool="query_defect_log",
                                 sig=nodes._call_sig({"group_by": "defect",
                                                      "device_id": "CX-104",
                                                      "window_days": None}))
        self.assertTrue(text.startswith("[query_defect_log{device_id=CX-104,group_by=defect}] "))
        self.assertEqual(nodes._call_sig({}), "")
        # 没有指纹就不打花括号，保持旧形状
        plain, _ = nodes._payload({"a": 1}, tool="query_defect_log")
        self.assertTrue(plain.startswith("[query_defect_log] "))

    def test_ref_with_signature_picks_the_right_call(self):
        # 两次返回都有 groups[0].rate，但值不同。带指纹能明确指出是哪一次——
        # 这正是档 C 之后剩下的那个盲点。
        ev = [
            {"ev_no": 9, "tool": "query_defect_log",
             "sig": "device_id=CX-104,group_by=station", "truncated": False,
             "result": json.dumps({"device_id": "CX-104",
                                   "groups": [{"rate": 0.4677, "key": "1号工位"}]})},
            {"ev_no": 10, "tool": "query_defect_log",
             "sig": "device_id=CX-104,group_by=defect", "truncated": False,
             "result": json.dumps({"device_id": "CX-104",
                                   "groups": [{"rate": 0.3469, "key": "划痕"}]})},
        ]
        doc = {"conclusion": "x", "claims": [
            {"text": "划痕类误判率 34.69%", "value": 34.69, "unit": "%",
             "ref": "query_defect_log{device_id=CX-104,group_by=defect}#groups[0].rate"}]}
        res = self._verify(doc, ev)
        self.assertEqual(res["misquoted"], [])
        self.assertEqual(res["cite_stats"]["cited_ok"], 1)

    def test_signature_mismatch_falls_back_instead_of_judging_dead(self):
        # 反向断言：指纹抄错（或那次调用被裁）时不能直接判 cite_oob，
        # 否则"参数抄错"会被当成"上下文裁剪"放过——那是开新口子。
        # 只在全部候选里按值继续判，值对不上才判失败。
        ev = [
            {"ev_no": 9, "tool": "query_defect_log",
             "sig": "device_id=CX-104,group_by=station", "truncated": False,
             "result": json.dumps({"groups": [{"rate": 0.4677}]})},
        ]
        doc = {"conclusion": "x", "claims": [
            {"text": "1号工位误判率 46.77%", "value": 46.77, "unit": "%",
             "ref": "query_defect_log{group_by=typo}#groups[0].rate"}]}
        res = self._verify(doc, ev)
        self.assertEqual(res["misquoted"], [], "指纹对不上但值对得上，不该判死")
        self.assertEqual(res["cite_stats"]["cited_ok"], 1)
        self.assertEqual(res["cite_stats"]["cite_oob"], 0)

        # 真错的（值也对不上）仍然要被抓住，不能因为指纹不匹配就放过
        doc["claims"][0]["value"] = 11.39
        res = self._verify(doc, ev)
        self.assertTrue(res["misquoted"], "值对不上必须判失败")

    def test_lookup_claims_use_tool_names(self):
        # lookup 走模板拼文本、不经 act，但它自产的 claims 必须和模型用同一套
        # 引用格式——否则校验层要同时理解两种语义，等于把编号问题搬回来。
        def fake_prof(device_id):
            return {"device_id": device_id, "maintenance_cycle_days": 90}

        def fake_log(device_id, group_by="station"):
            return {"total": 10,
                    "groups": [{"key": "1号工位", "count": 10,
                                "false_positive": 2, "rate": 0.2}]}

        def fake_wo(device_id):
            return {"state": "empty", "note": "没有工单记录"}

        state = {"device_ids": ["CX-104"], "turn": 2,
                 "evidence": [{"ev_no": i} for i in range(1, 4)]}
        with mock.patch.object(nodes, "query_device_profile", fake_prof), \
                mock.patch.object(nodes, "query_defect_log", fake_log), \
                mock.patch.object(nodes, "query_work_order", fake_wo):
            out = nodes.lookup(state)
        # ev_no 仍按上一轮的累计数续（审计用）：已有 3 条，新的必须从 4 起。
        self.assertEqual([e["ev_no"] for e in out["evidence"]], [4, 5, 6])
        self.assertEqual(len(out["evidence"]), 3)
        # 但 claims 里的 ref 必须是工具名，不能是这些编号
        # 用 _parse_claims 而不是 json.loads：final 可能是 dict（写入端已解析）
        # 也可能是字符串，_parse_claims 两种都吃。测试若耦合"一定是字符串"，
        # 下一次把某个节点改成存 dict，就会在这里误报成功能坏了。
        refs = [c.get("ref") for c in nodes._parse_claims(out["final"])["claims"]]
        self.assertTrue(refs, "lookup 必须自产 claims")
        for r in refs:
            self.assertRegex(r, r"^query_(device_profile|defect_log|work_order)#",
                             f"lookup 的 ref 应写工具名，实际是 {r}")


class LookupClaimsTest(unittest.TestCase):
    """lookup 的每个数字都要有 claim，且 ref 必须和取值出自同一处。

    lookup 没有模型介入，所以这里防的不是"模型编造"，而是
    **"拼接代码抄错字段"**——把 false_positive_rate 当成 rate 不会有任何报错：
    表格照常好看，数字照常是个数字，用户照常信。
    """

    PROF = {"device_id": "CX-104", "maintenance_cycle_days": 90, "log_count": 200,
            "camera_model": "CV-X450"}
    LOG = {"total": 107, "false_positive_count": 56, "false_positive_rate": 0.5234,
           "groups": [{"key": "1号工位", "count": 62, "false_positive": 29,
                       "rate": 0.4677}]}

    def _evidence(self):
        return [
            {"ev_no": 4, "tool": "query_device_profile",
             "result": json.dumps(self.PROF, ensure_ascii=False)},
            {"ev_no": 5, "tool": "query_defect_log",
             "result": json.dumps(self.LOG, ensure_ascii=False)},
        ]

    def test_every_claim_verifies(self):
        text, claims = nodes._data_block(self.PROF, self.LOG,
                                          "query_device_profile", "query_defect_log")
        stats, blocked, misquoted = nodes._new_stats(), [], []
        nodes._verify_claims({"claims": claims}, self._evidence(),
                             stats, blocked, misquoted)
        self.assertEqual(misquoted, [],
                         f"lookup 自己的 claim 过不了自己的校验：{misquoted}")
        self.assertEqual(stats["cited_ok"], len(claims))
        self.assertTrue("90 天" in text)       # 表格里的数字和 claim 是同一个值

    def test_wrong_ref_is_caught(self):
        # 反向断言：把 ref 指向同一条证据里的另一个字段，必须判失败。
        # 少了这条，"所有 ref 都指向证据根"也能让上一条通过。
        _, claims = nodes._data_block(self.PROF, self.LOG,
                                       "query_device_profile", "query_defect_log")
        bad = [dict(c) for c in claims]
        bad[0]["ref"] = "query_device_profile#log_count"   # 90 天 指向 200
        stats, blocked, misquoted = nodes._new_stats(), [], []
        nodes._verify_claims({"claims": bad}, self._evidence(),
                             stats, blocked, misquoted)
        self.assertTrue(misquoted, "ref 指向了字段、但值对不上，必须判失败")


class KnowledgeQuoteTest(unittest.TestCase):
    """knowledge 的结论是「手册规定 X」，风险不是数字编了，是**出处编了**。

    引文型 claim 让模型抄一段原文，程序回原文核对：
    Anthropic 的 `cited_text` 靠"模型给位置、程序去文档里切"来保证正确，
    我们让模型自己抄，就只能靠"抄完再查一遍"来保证。
    """

    CHUNK = ("当光源亮度设定低于 70 时，暗色工件边缘对比度下降约 15%，"
             "此时边缘类缺陷的过杀率会明显上升。")

    def _ev(self):
        return [{"ev_no": 7, "tool": "search_manual",
                 "result": json.dumps(
                     {"chunks": [{"text": self.CHUNK, "source_doc": "CV-X 手册",
                                  "section": "3.2"}]}, ensure_ascii=False)}]

    def _run(self, claims):
        stats, blocked, misquoted = nodes._new_stats(), [], []
        nodes._verify_claims({"claims": claims}, self._ev(), stats, blocked, misquoted)
        return stats, blocked, misquoted

    def test_quote_in_source_passes(self):
        # 允许引多段：两段都是原文，用省略号缝起来也算过
        claims = [{"text": "亮度低于 70 时对比度下降 15%",
                   "quote": "光源亮度设定低于 70 时……对比度下降约 15%",
                   "ref": "search_manual#chunks[0].text"}]
        stats, blocked, misquoted = self._run(claims)
        self.assertEqual(misquoted, [], f"真原文被当成假的：{misquoted}")
        self.assertEqual(stats["quote_ok"], 1)

    def test_drifted_quote_is_caught(self):
        # 反向断言：把原文的 70 写成 65，这句话在片段里就不存在了
        claims = [{"text": "亮度低于 65 时对比度下降",
                   "quote": "光源亮度设定低于 65 时", "ref": "search_manual#chunks[0].text"}]
        stats, blocked, misquoted = self._run(claims)
        self.assertTrue(misquoted, "引文被改了一个数字，必须判失败")
        self.assertEqual(stats["quote_bad"], 1)

    def test_ref_to_number_cannot_back_quote(self):
        # 反向断言：ref 指向一个数值，那里没有"原文"可引。
        # 少了这条，"引文随便挂到任何字段上"也能让前两条里的一条通过。
        claims = [{"text": "亮度低于 70 时对比度下降",
                   "quote": "光源亮度设定低于 70 时",
                   "ref": "search_manual#chunks[0].source_doc"}]
        stats, blocked, misquoted = self._run(claims)
        # source_doc 也是字符串，这里换成真正的数值字段再测一次
        claims[0]["ref"] = "search_manual#chunks[0].value"
        stats, blocked, misquoted = self._run(claims)
        self.assertTrue(misquoted, "ref 指向数值字段，撑不住一句引文")


class BareConclusionTest(unittest.TestCase):
    """结论里写了数字、claims 给空数组——这是刚发现的一条通道。

    它和"撤掉的那条豁免"是同一类洞的两侧：豁免是我们主动不检查，
    这个是模型把内容挪到了检查看不到的地方。两者都会让报告既不红也不绿。
    """

    def _run(self, doc):
        stats, blocked, misquoted = nodes._new_stats(), [], []
        nodes._verify_claims(doc, [], stats, blocked, misquoted)
        return stats, blocked, misquoted

    def test_numbers_without_claims_is_caught(self):
        doc = {"conclusion": "根因是光源衰减，误判率 52.34%，1 号工位 46.77%",
               "claims": []}
        stats, blocked, misquoted = self._run(doc)
        self.assertTrue(misquoted, "结论里的数字没有任何 claim 认领，必须判失败")
        self.assertEqual(stats["bare_conclusion"], 1)
        self.assertEqual(stats["qty"], 0)

    def test_clean_conclusion_without_claims_passes(self):
        # 反向断言：空 claims 本身不是罪。lookup 那句"不做根因判断"、
        # knowledge 那句"手册中未收录"，都是没有具体事实可引的结论，
        # 一律判失败会把它们误伤。
        doc = {"conclusion": "手册中未收录这条规则。", "claims": []}
        stats, blocked, misquoted = self._run(doc)
        self.assertEqual(misquoted, [])
        self.assertEqual(stats["bare_conclusion"], 0)

    def test_identifiers_are_not_numbers(self):
        # 真实撞到的误伤：手册答"没有 CX-104 这个型号"，104 被当成一个量，
        # 于是这句诚实的"未收录"被判成"写了数字却不给 claims"。
        # 设备号/告警码/日期都是标识符，和 verify 早先的口径一致，先剥掉再数。
        doc = {"conclusion": "手册中未收录 CX-104，也没有「标定漂移」的定义；"
                             "最接近的是 E-301，WO-20261116-004 与 11-07 的记录另见工单。",
               "claims": []}
        stats, blocked, misquoted = self._run(doc)
        self.assertEqual(misquoted, [], "纯标识符被当成量了")
        self.assertEqual(stats["bare_conclusion"], 0)

    def test_real_numbers_still_caught_after_stripping(self):
        # 反向断言：剥标识符不能顺手把真数字也剥掉。
        doc = {"conclusion": "CX-104 在 11-07 那天误判率 52.34%，超阈值 2 倍",
               "claims": []}
        stats, blocked, misquoted = self._run(doc)
        self.assertTrue(misquoted, "剥掉标识符之后，52.34% 和 2 倍必须还在")
        self.assertEqual(stats["bare_conclusion"], 1)

    def test_claims_present_does_not_trigger(self):
        # 反向断言：有 claims 时 conclusion 里出现数字不算违规——
        # 正常的结论会复述一句关键数字，认领它的 claim 在那摆着。
        doc = {"conclusion": "误判率 52.34%，根因偏硬件",
               "claims": [{"text": "整体误判率 52.34%", "value": 52.34,
                           "unit": "%", "ref": "query_defect_log#false_positive_rate"}]}
        log = {"false_positive_rate": 0.5234}
        stats, blocked, misquoted = nodes._new_stats(), [], []
        nodes._verify_claims(doc,
                             [{"ev_no": 5, "tool": "query_defect_log",
                               "result": json.dumps(log)}],
                             stats, blocked, misquoted)
        self.assertEqual(stats["bare_conclusion"], 0)
        self.assertEqual(stats["cited_ok"], 1)


    def test_svc_fail_is_judged_as_half_result(self):
        # 兜底文案的形状是合法结论（claims 空、conclusion 无数字），
        # 只看形状会静默通过。但那段话里没有任何被整理出来的内容，
        # 拿它当"通过"就是把一次超时记成了成功。
        state = {"final": nodes._prose_doc("（整理答案时调用失败：APITimeoutError）未整理"),
                 "svc_fail": "APITimeoutError", "evidence": [], "turn": 1}
        out = nodes.verify(state)
        self.assertTrue(out["misquoted"], "服务失败必须判失败，否则这条废结论会被当成通过")
        self.assertEqual(out["cite_stats"]["svc_fail"], 1)
        # 而且不能被记成"引用写错"：那是模型的责任，这条是基础设施的责任
        self.assertFalse(out["cite_stats"]["json_bad"])

    def test_clean_run_has_no_svc_fail(self):
        state = {"final": nodes._prose_doc("手册中未收录这条规则。"),
                 "svc_fail": "", "evidence": [], "turn": 1}
        out = nodes.verify(state)
        self.assertEqual(out["misquoted"], [])
        self.assertEqual(out["cite_stats"]["svc_fail"], 0)


class TurnResetTest(unittest.TestCase):
    """intake 每轮必须把"本轮字段"清干净。

    三轮探针撞上过一次：轮 2 走 knowledge 留下 final_text，轮 3 走 diagnose，
    verify 用 preset_text 优先，于是 UI 上显示的是**上一轮的结论**，
    而 final（claims）是本轮的、校验还全绿。指标全绿，人一看文本才知道答错了题。

    check_notes 是同一个坑的另一半：它会被 verify 并进 misquoted，
    不清就会让上一轮的回环问题在这一轮再判一次失败。
    """

    def test_intake_clears_turn_fields(self):
        prev = {
            "final": "上一轮的结论", "final_text": "上一轮的可读文本",
            "misquoted": ["上一轮的伪引用"], "cite_stats": {"qty": 3},
            "check_notes": ["上一轮回环把 ref 改成 null"],
            "check_retry": 1, "check_action": "retry",
            "check_unsourced0": 2, "svc_fail": "APITimeoutError",
            "stop_reason": "budget_exhausted", "blocked": ["旧"],
            "tool_names": ["query_calibration"], "steps": 9, "plan_retry": 2,
            "turn": 1, "device_id": "CX-104",
        }
        out = nodes.intake(prev)
        for key in ("final", "final_text", "check_action", "svc_fail", "stop_reason"):
            self.assertEqual(out.get(key), "", f"{key} 没被清空")
        for key in ("misquoted", "check_notes", "blocked"):
            self.assertEqual(out.get(key), [], f"{key} 没被清空")
        for key in ("cite_stats",):
            self.assertEqual(out.get(key), {}, f"{key} 没被清空")
        for key in ("check_retry", "check_unsourced0", "steps", "plan_retry"):
            self.assertEqual(out.get(key), 0, f"{key} 没被清零")

    def test_stale_final_text_does_not_override_this_turn(self):
        # 本条走一轮 diagnose 形态的结论，没有 preset_text，
        # 必须按本轮 doc 渲染，而不是沿用上一轮的字符串。
        doc = {"conclusion": "标定漂移超阈值", "claims": [], "advice": []}
        state = {"final_text": "上一轮：手册里没有 CX-104", "turn": 1}
        # 真实节点顺序：intake 先清 -> 节点写 final -> verify
        # 所以要先清、再塞本轮的 final，不能反过来。
        state.update(nodes.intake(state))
        state["final"] = json.dumps(doc, ensure_ascii=False)
        out = nodes.verify(state)
        self.assertNotIn("手册里没有 CX-104", out["final_text"])
        self.assertIn("标定漂移", out["final_text"])


class ResultEventContractTest(unittest.TestCase):
    """result 事件的两个出口（CLI/评测 与 Web 流式）必须写同样的字段。

    分头写过一次：server._sse 只写 final_head，不写 final 全文。
    同一次运行，走 CLI 能离线复算「这条结论为什么被/没被拦」，走 Web 不能——
    而且不会有任何报错，只有在想回溯线上问题时才发现证据不全，
    那时重跑的结果已经不一定一样了。
    """

    REQUIRED = {"turn", "intent", "intent_conf", "steps", "stop_reason", "check_rounds",
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


    def test_done_frame_derives_from_result_event(self):
        # done 帧原来手抄 12 个字段，和 result_event 重合 9 个：
        # state 改一个名字要记得改两处，漏了不报错，只静默少一个字段。
        # 这条测的是"派生"——逐字段比对公共部分，抄一遍过不了。
        from app.web import server as web_server
        st = {"turn": 3, "intent": "diagnose", "steps": 4, "tool_names": ["a"],
              "blocked": ["x"], "misquoted": [], "device_ids": ["CX-104"],
              "slots": {"device_id": "CX-104"}, "final": "{}", "final_text": "x",
              "cite_stats": {"qty": 1, "cited_ok": 1},
              "stop_reason": "budget_exhausted", "check_retry": 1}
        audit = build.result_event(st)
        frame = web_server._done_event(st, trace_id="r1", thread_id="t1", elapsed_ms=7)
        self.assertEqual(frame["type"], "done")
        for k, v in audit.items():
            self.assertEqual(frame[k], v, f"done 帧的 {k} 与 result_event 不一致")
        # 前端真的会读这几个（index.html 的 renderDone）
        for k in ("evidence", "trace_id", "total_ms", "cite", "blocked", "final_text"):
            self.assertIn(k, frame, f"done 帧少了前端要读的 {k}")
        # stop_reason 以前到不了 UI，于是分不清"完整结论"和"被预算掐断的半成品"
        self.assertEqual(frame["stop_reason"], "budget_exhausted")


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


class JudgeAttributionTest(unittest.TestCase):
    """失败要能分清"没答 / 追问 / 答偏"。

    盯的不是判对错，是红了之后看不看得出病因。

    T3 三次记录全落进同一句 reasons（判断错/结论未命中），但实际是：
      ae28aa2  追问"你想先看哪一方面"                  → 没答
      a553cc6  追问 + 把"意图置信度 0.55"打进答案里     → 没答，还漏了内部字段
      32704d4  真去查了三个工具，列 15 条真实数字       → 答偏（而且是抗令，见下）

    前两种该改话术，第三种该改用例或改"该不该查"的判定，第四种要改模型。
    **一个字符串盖住四件事，等于把诊断推给看报告的人。**

    注意 T3 的第三形态判据只能给到 conclusion_off，判不出"抗令"：
    用例里没有"禁止调用"的字段，判据不知道这些调用本来是不该发生的。
    这条边界写在 failure 的 note 里，不假装判得出来。
    """

    BASE_TRAJ = {
        "id": "X", "type": "陷阱", "expect_tools": [], "forbid_tools": [],
        "expect_group_by": [], "final_keywords": ["无法", "没有", "不能"],
        "expect_ask": False, "steps_range": (0, 8),
    }

    def _judge(self, traj=None, **kw):
        st = {"tool_names": [], "final_text": "x", "steps": 1, "blocked": [],
              "misquoted": [], "cite_stats": {}, "evidence": [],
              "intent": "lookup", "intent_conf": 0.9,
              "stop_reason": "enough", "check_retry": 0}
        st.update(kw)
        return run_eval.judge(traj or dict(self.BASE_TRAJ), st)

    def test_asked_instead_of_answering_is_named(self):
        # 追问本身没错，但这条用例要求直接给结论 → 失败原因必须是"追问了"
        r = self._judge(intent="clarify", tool_names=[],
                        final_text="CX-101 你想先看哪一方面？")
        self.assertFalse(r["semantic_ok"])
        self.assertEqual([f["code"] for f in r["failure"]], ["asked_instead"])

    def test_called_tools_but_missed_keywords_is_named_off(self):
        # 调了工具、结论却没落在关键词上：和"啥也没做"分开
        r = self._judge(tool_names=["query_device_profile", "query_defect_log"],
                        final_text="按设备档案与误判统计列出如下")
        self.assertEqual([f["code"] for f in r["failure"]], ["conclusion_off"])
        self.assertEqual(r["failure"][0]["basis"]["tools"],
                         ["query_device_profile", "query_defect_log"])
        # 边界要说出来：判据不知道这些调用是否多余
        self.assertIn("看不出", r["failure"][0]["note"])

    def test_nothing_done_is_plain_miss(self):
        r = self._judge(tool_names=[], final_text="不知道")
        self.assertEqual([f["code"] for f in r["failure"]], ["conclusion_miss"])

    def test_pass_has_no_failure_entries(self):
        traj = dict(self.BASE_TRAJ, final_keywords=["3"])
        r = self._judge(traj=traj, final_text="CX-101 是 3 个工位")
        self.assertTrue(r["semantic_ok"])
        self.assertEqual(r["failure"], [])

    def test_every_reason_still_has_a_code(self):
        # 兼容约束：reasons 一条都不能少。结构性归因是加在旁边的，
        # 不是替换掉给人看的那句话。
        traj = dict(self.BASE_TRAJ, expect_tools=["query_device_profile"],
                    forbid_tools=["create_work_order"], steps_range=(5, 6))
        r = self._judge(traj=traj, tool_names=[], steps=0)
        self.assertEqual(len(r["failure"]), len(r["reasons"]))
        self.assertTrue(all(f["code"] for f in r["failure"]))
        self.assertTrue(all(f["owner"] in ("model", "case", "uncertain")
                            for f in r["failure"]))


class StateResetManifestTest(unittest.TestCase):
    """本轮字段的清单只有一份：state.RESET_EACH_TURN。

    守的是"加字段时忘了登记"。原来 intake 手抄 14 行，漏一个的代价是
    上一轮的值污染本轮——而指标全绿，只有人看文本才知道答的不是同一件事。
    """

    def test_every_reset_key_is_declared_in_state(self):
        declared = set(graph_state.State.__annotations__)
        missing = set(graph_state.RESET_EACH_TURN) - declared
        self.assertEqual(
            missing, set(),
            f"这些字段在 RESET_EACH_TURN 里，但 State 没声明：{sorted(missing)}")

    def test_reset_values_are_fresh_each_call(self):
        # list / dict 必须每次是新对象。共享一份的话，下游 append 一次，
        # 下一轮一开跑就带着上一轮的内容——正是这张表要防的事。
        a = graph_state.fresh_reset_values()
        b = graph_state.fresh_reset_values()
        a["blocked"].append("x")
        a["cite_stats"]["k"] = 1
        self.assertEqual(b["blocked"], [])
        self.assertEqual(b["cite_stats"], {})
        # check_ev 的初值必须是 None 而不是 0：0 是"跑过 check、看到 0 条证据"，
        # 清成 0 会让 verify 在 lookup / knowledge 路径上误记一条 ev_mismatch。
        self.assertIsNone(b["check_ev"])


class SessionSummaryCapTest(unittest.TestCase):
    """跨轮摘要必须有上限：它是唯一一个"每轮加一行、从不清理"的字段，
    而 plan 又整段塞进 prompt —— 会话一长，每一轮都在为很久以前付 token。
    """

    def test_keeps_only_last_n_lines(self):
        text = ""
        for i in range(1, 21):
            text = graph_state._tail_lines(text, f"\n[{i}] 轮{i}")
        lines = text.splitlines()
        self.assertEqual(len(lines), graph_state.SESSION_SUMMARY_KEEP)
        self.assertEqual(lines[0], f"[{20 - graph_state.SESSION_SUMMARY_KEEP + 1}] "
                                   f"轮{20 - graph_state.SESSION_SUMMARY_KEEP + 1}")
        self.assertEqual(lines[-1], "[20] 轮20")

    def test_dropping_is_by_line_not_by_chars(self):
        # 按字符切会把某行的后半句留在开头，读起来像一条完整记录，其实是半句话。
        text = graph_state._tail_lines("", "\n[1] " + "长" * 300)
        for i in range(2, 11):
            text = graph_state._tail_lines(text, f"\n[{i}] y")
        self.assertTrue(text.startswith("[3]"), text[:12])
        self.assertNotIn("长", text, "旧行必须整行消失，不能留半句")

    def test_under_limit_is_untouched(self):
        text = ""
        for i in range(1, 4):
            text = graph_state._tail_lines(text, f"\n[{i}] 轮{i}")
        self.assertEqual(text.splitlines(), ["[1] 轮1", "[2] 轮2", "[3] 轮3"])

    def test_state_annotation_actually_points_to_this_reducer(self):
        # 上面三条测的是函数本身。这条测"LangGraph 合并时拿到的确实是它"——
        # 改了函数却忘了改 Annotated，三条全绿而运行时仍是无限拼接。
        import typing
        hints = typing.get_type_hints(graph_state.State, include_extras=True)
        meta = hints["session_summary"].__metadata__
        self.assertTrue(meta, "session_summary 没有 reducer，LangGraph 会直接覆盖")
        self.assertIs(meta[0], graph_state._tail_lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
