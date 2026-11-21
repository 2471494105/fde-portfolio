# -*- coding: utf-8 -*-
"""飞书入口骨架（DOC-37 W2）的单测：归一 / 判重语义 / 卡片形状。

跑法（项目根）：
    venv\\Scripts\\python.exe -m unittest discover -s tests -t tests -p "test_*.py"

不连飞书、不调模型：事件用 SimpleNamespace 造，卡片只断言结构。
只有 DedupeTest 会碰 PG，它自己探一次连接，连不上就 skip——
单测不该因为"这台机器没起数据库"变红。
"""

import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.entry.feishu import config, inbound, render
from app.entry.feishu.dedupe import Dedupe


def _event(text="CX-101 又误报了", msg_type="text", open_id="ou_1",
           chat_id="oc_1", message_id="om_1", sender_type="user",
           mentions=(), thread_id=""):
    """按 SDK 的字段形状造一个 im.message.receive_v1 事件。

    形状抄自 lark_oapi/api/im/v1/model/p2_im_message_receive_v1.py：
    event.{sender, message}，message.content 是 JSON 字符串（不是 dict）。
    """
    msg = SimpleNamespace(
        message_id=message_id, chat_id=chat_id, thread_id=thread_id,
        chat_type="group", message_type=msg_type,
        content=json.dumps({"text": text}, ensure_ascii=False),
        mentions=list(mentions), create_time=1763700000,
    )
    sender = SimpleNamespace(
        sender_id=SimpleNamespace(open_id=open_id, union_id="", user_id=""),
        sender_type=sender_type, tenant_key="t1",
    )
    return SimpleNamespace(event=SimpleNamespace(message=msg, sender=sender))


def _mention(key="@_user_1", name="诊断助手"):
    return SimpleNamespace(key=key, name=name, id=None)


class InboundTest(unittest.TestCase):
    """事件 → 七个字段，且 @机器人 的占位符要抠掉。"""

    def test_text_strips_mention_placeholder(self):
        ev = _event(text="@_user_1 CX-101 又误报了", mentions=[_mention()])
        msg, why = inbound.parse(ev)
        self.assertEqual(why, "")
        # 占位符留着的话，understand 会把 "@_user_1" 当成设备名的一部分
        self.assertEqual(msg.text, "CX-101 又误报了")
        self.assertEqual(msg.user_key, "ou_1")
        self.assertEqual(msg.room_key, "oc_1")
        self.assertEqual(msg.event_id, "om_1")
        self.assertEqual(msg.channel, "feishu")

    def test_raw_carries_only_log_fields(self):
        msg, _ = inbound.parse(_event())
        # raw 是给日志的，不该出现 open_id / chat_id 这类平台身份
        self.assertEqual(set(msg.raw), {"chat_type", "message_type", "create_time"})

    def test_non_text_is_skipped_with_reason(self):
        for mtype in ("post", "image", "file"):
            msg, why = inbound.parse(_event(msg_type=mtype))
            self.assertIsNone(msg)
            self.assertEqual(why, f"unsupported-type:{mtype}")

    def test_app_sender_is_skipped(self):
        # 机器人之间会互相触发，它不是提问方
        msg, why = inbound.parse(_event(sender_type="app"))
        self.assertIsNone(msg)
        self.assertEqual(why, "from-app")

    def test_missing_open_id_is_skipped(self):
        msg, why = inbound.parse(_event(open_id=""))
        self.assertIsNone(msg)
        self.assertEqual(why, "missing-identity")

    def test_whitespace_only_text_is_skipped(self):
        msg, why = inbound.parse(_event(text="   "))
        self.assertIsNone(msg)
        self.assertEqual(why, "empty-text")

    def test_thread_id_is_namespaced_and_per_user(self):
        a = inbound.thread_id("oc_1", "ou_a")
        b = inbound.thread_id("oc_1", "ou_b")
        self.assertEqual(a, inbound.thread_id("oc_1", "ou_a"))
        self.assertNotEqual(a, b)          # 同群两个人，各一条会话
        self.assertTrue(a.startswith("fs-"))
        # 16 个十六进制 + 前缀；Web 的裸 uuid 不会被撞上
        self.assertEqual(len(a), len("fs-") + 16)


class ConfigTest(unittest.TestCase):
    """缺凭据必须能被点名，不能静默降级。"""

    def setUp(self):
        # .env 里若真写了 FEISHU_*，_load_env 会把它们 setdefault 回来；这里把它禁掉，
        # 测的是"缺失判定"本身，不是这台机器的 .env 长什么样
        self._patch = mock.patch.object(config, "_load_env", lambda: None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _env(self, **kw):
        p = mock.patch.dict(os.environ, kw, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def test_missing_lists_both_when_absent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in config.REQUIRED + config.OPTIONAL:
                os.environ.pop(k, None)
            self.assertEqual(config.missing(), ["FEISHU_APP_ID", "FEISHU_APP_SECRET"])

    def test_load_raises_with_keys(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in config.REQUIRED:
                os.environ.pop(k, None)
            with self.assertRaises(config.MissingCreds) as ctx:
                config.load()
            self.assertEqual(ctx.exception.keys, ["FEISHU_APP_ID", "FEISHU_APP_SECRET"])

    def test_load_ok_and_optionals_default_empty(self):
        self._env(FEISHU_APP_ID=" cli_1 ", FEISHU_APP_SECRET="s1")
        creds = config.load()
        self.assertEqual(creds.app_id, "cli_1")      # 两侧空白要 strip
        self.assertEqual(creds.app_secret, "s1")
        self.assertEqual(creds.verification_token, "")
        self.assertEqual(creds.encrypt_key, "")


class RenderTest(unittest.TestCase):
    """帧 → 卡片 2.0：结构、颜色、折叠、截断。"""

    def _frames(self):
        return [
            {"type": "open", "thread_id": "fs-x"},
            {"type": "node_start", "node": "understand", "label": "理解你的问题", "t": 30},
            {"type": "node_end", "node": "understand", "label": "理解你的问题",
             "ms": 5200, "detail": "意图：执行动作（建单）", "t": 5230},
            {"type": "node_start", "node": "act", "label": "执行查询", "t": 5300},
            {"type": "node_end", "node": "act", "label": "执行查询",
             "ms": 80, "detail": "查询缺陷记录 已返回", "t": 5400},
            {"type": "done", "final_cn": "CX-101 主要是镜头污染。", "evidence": [
                {"tool": "query_defect_log", "tool_cn": "查询缺陷记录",
                 "summary": "近 7 天 3 次", "current": True},
            ], "total_ms": 7500},
        ]

    def test_card_shape(self):
        card = render.build(self._frames())
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["update_multi"])   # 共享卡片才可能被 PATCH
        self.assertEqual(card["header"]["title"]["tag"], "plain_text")
        json.dumps(card, ensure_ascii=False)              # 必须能序列化
        elements = card["body"]["elements"]
        self.assertTrue(any(e["tag"] == "hr" for e in elements))
        self.assertEqual(card["header"]["template"], "green")   # 有结论

    def test_progress_line_keeps_repeats(self):
        frames = [
            {"type": "node_start", "node": "act", "label": "执行查询"},
            {"type": "node_end", "node": "act", "label": "执行查询", "ms": 10, "detail": "a"},
            {"type": "node_start", "node": "act", "label": "执行查询"},
            {"type": "node_end", "node": "act", "label": "执行查询", "ms": 20, "detail": "b"},
        ]
        lines = render.progress_lines(frames)
        # plan/act 是循环：跑两轮就该是两行，按 node 名去重会让现场看到假的"一次"
        self.assertEqual(len(lines), 2)
        self.assertEqual([ln["ms"] for ln in lines], [10, 20])

    def test_unfinished_line_shows_waiting(self):
        card = render.build([{"type": "node_start", "node": "plan", "label": "判断下一步查什么"}])
        md = card["body"]["elements"][0]["content"]
        self.assertIn("⏳", md)
        self.assertIn("判断下一步查什么", md)
        self.assertEqual(card["header"]["template"], "blue")     # 处理中

    def test_error_turns_red_and_has_no_conclusion(self):
        card = render.build([{"type": "error", "message": "RuntimeError: boom"}])
        self.assertEqual(card["header"]["template"], "red")
        md = "\n".join(e.get("content", "") for e in card["body"]["elements"])
        self.assertIn("执行中断", md)
        self.assertIn("boom", md)

    def test_long_progress_folds(self):
        frames = [{"type": "node_start", "node": "act", "label": f"步骤{i}"} for i in range(14)]
        md = render.build(frames)["body"]["elements"][0]["content"]
        self.assertIn(f"另有 {14 - render.MAX_PROGRESS} 步已折叠", md)

    def test_conclusion_is_truncated(self):
        card = render.build([{"type": "done", "final_cn": "长" * 3000}])
        md = [e["content"] for e in card["body"]["elements"] if e.get("content", "").startswith("长")][0]
        self.assertLessEqual(len(md), render.MAX_CLAIM + len("…（已截断）"))
        self.assertIn("已截断", md)

    def test_evidence_prefers_current_round(self):
        frames = [{"type": "done", "final_cn": "x", "evidence": [
            {"tool": "a", "summary": "旧", "current": False},
            {"tool": "b", "summary": "新", "current": True},
        ]}]
        md = [e["content"] for e in render.build(frames)["body"]["elements"]
              if "本轮依据" in e.get("content", "")][0]
        self.assertIn("新", md)
        self.assertNotIn("旧", md)


class ReadySignalTest(unittest.TestCase):
    """ready 行的触发条件。

    这是 W2 里唯一没法在没有真应用时端到端验证的东西（要一条真长连接才能看到
    SDK 打 "connected to ..."）。所以退一步：把"什么算连上"这一条判断单独测掉，
    真机上要验的就只剩"SDK 到底会不会打那行"。
    """

    def _handler(self):
        # 从 ready.py 取，不从 __main__ 取：后者一导入就拖进整个 lark_oapi（15.5 s）
        from app.entry.feishu.ready import ReadyOnConnected
        return ReadyOnConnected()

    @staticmethod
    def _rec(msg):
        import logging as _l
        return _l.LogRecord("Lark", _l.INFO, __file__, 1, msg, None, None)

    def test_connected_log_prints_ready_once(self):
        h = self._handler()
        with mock.patch("builtins.print") as p:
            h.emit(self._rec("connected to wss://example/x?device_id=1&service_id=2"))
            h.emit(self._rec("connected to wss://example/x"))          # 断线重连会再打一次
        self.assertEqual(p.call_count, 1)
        self.assertIn("ready: ws connected, db ok", p.call_args[0][0])
        self.assertTrue(h.done)

    def test_other_logs_do_not_trigger(self):
        h = self._handler()
        with mock.patch("builtins.print") as p:
            h.emit(self._rec("connect failed, err: 1000040346: app_id is invalid"))
        p.assert_not_called()
        self.assertFalse(h.done)

    def test_mask_secrets_hides_credentials_but_keeps_ids(self):
        from app.entry.feishu.ready import MaskSecrets
        rec = self._rec("connected to wss://msg-frontier.feishu.cn/ws/v2?fpid=493"
                        "&access_key=abc123&service_id=33554678&ticket=def-456")
        MaskSecrets().filter(rec)
        msg = rec.getMessage()
        self.assertIn("access_key=***", msg)
        self.assertIn("ticket=***", msg)
        self.assertNotIn("abc123", msg)
        self.assertNotIn("def-456", msg)
        # 非凭证参数要留着：service_id 是排障时唯一能对上的线索
        self.assertIn("service_id=33554678", msg)

    def test_mask_secrets_keeps_ready_signal(self):
        # 打码与"算不算连上"发生在同一条 record 上，不能互相打死
        from app.entry.feishu.ready import MaskSecrets
        h = self._handler()
        rec = self._rec("connected to wss://x/ws/v2?access_key=k&ticket=t")
        MaskSecrets().filter(rec)
        with mock.patch("builtins.print") as p:
            h.emit(rec)
        p.assert_called_once()


class DedupeTest(unittest.TestCase):
    """入口判重：同键第二次必须返回 True。"""

    @classmethod
    def setUpClass(cls):
        try:
            Dedupe().setup()
            cls._ok = True
        except Exception as e:      # 没起 PG 就 skip，不让环境问题变成红
            cls._why = f"{type(e).__name__}: {e}"
            cls._ok = False

    def setUp(self):
        if not self._ok:
            self.skipTest(f"PG 不可用（{self._why}）")

    def test_first_false_then_true(self):
        d = Dedupe()
        key = f"w2-{uuid.uuid4().hex[:8]}"
        self.assertFalse(d.seen("feishu", key))
        self.assertTrue(d.seen("feishu", key))

    def test_same_key_different_channel_does_not_collide(self):
        d = Dedupe()
        key = f"w2-{uuid.uuid4().hex[:8]}"
        self.assertFalse(d.seen("feishu", key))
        # 将来 Web 也走同一张表时，两个通道的同名键不能互相误杀
        self.assertFalse(d.seen("web", key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
