"""造三个"一直为空、形态没被验证过"的字段。

背景：`state_samples_20261118.json` 里 check_notes / svc_fail / intent_note
在四条真实 case 上全是空值。空值没有说明力——声明写错、读端猜错，
都不会有一条红测试告诉你。这三个字段各自只在一条分支上出现：

  check_notes  回环后未溯源数字不降反涨（check_retry 已达上限）
  svc_fail     knowledge 整理答案那步模型调用失败（except 分支）
  intent_note  意图置信度落在 [CONF_ASK, CONF_OK) 的免责区间

三者都不依赖真实模型和网络，可以确定性造出来：

  check_notes  调 check()，构造 nullswap 的 state —— 纯程序节点
  svc_fail     调 knowledge()，把 search_manual 钉成固定 chunks、
               把 get_client 换成一调用就抛的假 client
  intent_note  调 understand()，把 get_client 换成返回低置信 JSON 的假 client

这不是"假装跑了一遍"：函数真的执行了，走的是真实分支，写出来的就是真实
结构。差别只在于外部 I/O 被替换成确定性替身——这也是离线复现这两条错误
分支的唯一办法。

样本的 sample_case 一律以 construct_ 开头，好和真实会话区分开。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph import nodes  # noqa: E402

OUT = ROOT / "docs" / "state_samples_20261118.json"


# ---- 假模型客户端：只提供 chat.completions.create 这一条被用到的路径 ----

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _Completions:
    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc

    def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return _Resp(self._content)


class _Chat:
    def __init__(self, **kwargs):
        self.completions = _Completions(**kwargs)


class _Client:
    def __init__(self, **kwargs):
        self.chat = _Chat(**kwargs)


# ---- 三个构造 ----

def build_intent_note(conf=0.68, intent="diagnose", device="CX-101"):
    """置信度 0.68 落在 [0.6, 0.75)：不追问，但在结论里带免责说明。"""
    payload = json.dumps({
        "intent": intent,
        "confidence": conf,
        "device_ids": [device],
        "phenomenon": "夜班误判率一直偏高",
        "missing": [],
        "reason": "用户在问某个设备的误判率为何异常",
    }, ensure_ascii=False)
    nodes.get_client = lambda: _Client(content=payload)
    st = {"user_msg": "CX-101 的夜班误判率为什么一直偏高，帮我查下", "device_id": ""}
    return nodes.understand(st)


def build_svc_fail():
    """检索跑通、模型调用超时：兜底文案必须长成同一个形状，并带 svc_fail 标记。"""
    nodes.search_manual = lambda q, top_k=5: {
        "chunks": [{"source_doc": "CX-101 维护手册", "section": "3.2 标定周期",
                    "text": "标定周期为 90 天。"}],
    }
    nodes.get_client = lambda: _Client(exc=TimeoutError("上游 504"))
    st = {"user_msg": "CX-101 标定周期多久", "param_names": [], "evidence": [], "turn": 1}
    return nodes.knowledge(st)


def build_check_notes():
    """回环一次后 unsourced 从 0 涨到 1：把指错的 ref 改成 null 不算修正。"""
    st = {
        "turn": 1,
        "evidence": [{
            "tool": "query_device_profile", "args": {"device_id": "CX-101"}, "sig": "",
            "result": json.dumps({"device_id": "CX-101", "maintenance_cycle_days": 90}),
            "truncated": False, "ev_no": 1, "turn": 1,
        }],
        "tool_names": ["query_device_profile"],
        "unknown": [], "conflicts": [],
        "final": {
            "conclusion": "保养周期 90 天，另有一处数据待核。",
            "claims": [
                {"text": "保养周期 90 天", "value": 90, "unit": "天",
                 "ref": "query_device_profile#maintenance_cycle_days"},
                {"text": "推定 3 台", "value": 3, "unit": "台", "ref": None},
            ],
            "advice": [],
        },
        "check_retry": 1,      # 已达 MAX_CHECK_RETRY
        "check_unsourced0": 0,  # 上一轮没有未溯源数字
    }
    return nodes.check(st)


def main():
    data = json.loads(OUT.read_text(encoding="utf-8"))
    fields = data["fields"]

    it = build_intent_note()
    fields["intent_note"]["sample"] = it["intent_note"]
    fields["intent_note"]["sample_case"] = "construct_intent_conf068"
    fields["intent_note"]["seen_in"] = ["diagnose_ok", "construct_intent_conf068"]

    sf = build_svc_fail()
    fields["svc_fail"]["sample"] = sf.get("svc_fail")
    fields["svc_fail"]["sample_case"] = "construct_svc_timeout"
    fields["svc_fail"]["seen_in"] = ["diagnose_ok", "construct_svc_timeout"]
    # knowledge 遇到模型失败时 final 是 prose dict，这里一并留证
    fields["svc_fail"]["shape"] = "str（异常类名，空串表示本轮没失败）"

    ck = build_check_notes()
    fields["check_notes"]["sample"] = ck.get("check_notes") or []
    fields["check_notes"]["sample_case"] = "construct_nullswap"
    fields["check_notes"]["seen_in"] = ["diagnose_ok", "construct_nullswap"]
    fields["check_notes"]["shape"] = "list[str]"

    data["generated_for"] = data.get("generated_for") or ""
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("intent_note =", it["intent_note"])
    print("intent / conf =", it["intent"], "/", it["intent_conf"])
    print("svc_fail    =", sf.get("svc_fail"))
    print("svc final   =", json.dumps(sf.get("final"), ensure_ascii=False)[:120])
    print("check_notes =", ck.get("check_notes"))
    print("check action=", ck.get("check_action"), "retry=", ck.get("check_retry"))
    print(f"\n已并入 {OUT}")


if __name__ == "__main__":
    main()
