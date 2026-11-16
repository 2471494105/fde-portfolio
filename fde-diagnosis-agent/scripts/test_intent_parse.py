"""验证 understand 的 JSON 容错（修复前：裹 ```json 的返回必然退化成 fallback）。

跑法：
    venv\\Scripts\\python.exe scripts\\test_intent_parse.py

不联网：LLM 调用全部用 mock 替身，专门构造旧写法会失败的输入。
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

# 脚本在 scripts/ 下执行，sys.path[0] 是 scripts/，需要把项目根补上才能 import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.graph.nodes import _parse_json_loose, understand


def fake_resp(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def run_understand(replies):
    """replies: 依次返回的内容；元素为 Exception 时表示该次调用抛异常。"""
    state = {"user_msg": "CX-101 上次那个工单处理了吗", "device_id": "",
             "last_question": "", "slots": {}}
    calls = {"n": 0}

    def create(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        r = replies[min(i, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return fake_resp(r)

    with mock.patch("app.graph.nodes.get_client") as g:
        g.return_value.chat.completions.create.side_effect = create
        st = understand(state)
    return st, calls["n"]


def main():
    print("=" * 60)
    print("一、_parse_json_loose 单元层")
    print("=" * 60)
    cases = [
        ("A 纯 JSON", '{"intent":"lookup","device_ids":["CX-101"]}'),
        ("B ```json 包裹", '```json\n{"intent":"lookup","device_ids":["CX-101"]}\n```'),
        ("C 前导说明文字", '好的，判断如下：\n{"intent":"lookup","device_ids":["CX-101"]}'),
        ("D 完全非 JSON", '我不知道该怎么判断'),
        ("E 空字符串", ''),
        ("F 截断的 JSON", '{"intent":"look'),
    ]
    for name, raw in cases:
        got = _parse_json_loose(raw)
        status = "救回" if got is not None else "放弃(预期)"
        val = got.get("intent") if isinstance(got, dict) else None
        print(f"  {name:18s} -> {status}" + (f" intent={val}" if val else ""))

    print()
    print("=" * 60)
    print("二、understand 端到端（mock LLM）")
    print("=" * 60)

    # S1：第一次裹壳（旧写法在这里就崩），第二次纯 JSON → 应正常判为 lookup
    st, n = run_understand([
        '```json\n{"intent":"lookup","device_ids":["CX-101"],"phenomenon":""}\n```',
        '{"intent":"lookup","device_ids":["CX-101"],"phenomenon":"","missing":[],"reason":"查工单"}',
    ])
    print(f"  S1 首次裹壳           -> intent={st['intent']:9s} 调用{n}次  reason={st['intent_reason'][:46]}")
    assert st["intent"] == "lookup", "S1 应判为 lookup"
    assert n == 1, f"S1 裹壳应被容错救回、不该重试，实际调用 {n} 次"

    # S2：两次都不是 JSON → 重试一次后 fallback，且理由要写明是解析问题
    st, n = run_understand(['我不知道', '我还是不知道'])
    print(f"  S2 两次都非 JSON      -> intent={st['intent']:9s} 调用{n}次  reason={st['intent_reason'][:46]}")
    assert n == 2, f"S2 应恰好重试一次（共 2 次调用），实际 {n}"
    assert "不是合法 JSON" in st["intent_reason"], "S2 理由应说明解析失败"

    # S3：调用直接抛异常 → 不重试，立刻 fallback
    st, n = run_understand([TimeoutError("timeout"), '{"intent":"lookup"}'])
    print(f"  S3 调用抛异常         -> intent={st['intent']:9s} 调用{n}次  reason={st['intent_reason'][:46]}")
    assert n == 1, f"S3 网络/超时失败不该重试，实际调用 {n} 次"
    assert "调用失败" in st["intent_reason"]

    # S4：正常纯 JSON，确认没被容错逻辑改坏
    st, n = run_understand(['{"intent":"knowledge","device_ids":[],"phenomenon":"","missing":[],"reason":"问告警含义"}'])
    print(f"  S4 正常 JSON          -> intent={st['intent']:9s} 调用{n}次  reason={st['intent_reason'][:46]}")
    assert st["intent"] == "knowledge"
    assert n == 1

    print()
    print("=" * 60)
    print("三、对照组：旧写法（直接 json.loads），证明问题真实存在")
    print("=" * 60)
    legacy_inputs = [
        ("B ```json 包裹", '```json\n{"intent":"lookup","device_ids":["CX-101"]}\n```'),
        ("C 前导说明文字", '好的，判断如下：\n{"intent":"lookup","device_ids":["CX-101"]}'),
    ]
    for name, raw in legacy_inputs:
        try:
            json.loads(raw)
            print(f"  {name:18s} -> 旧写法也能过（与预期不符，需复核）")
        except json.JSONDecodeError as e:
            print(f"  {name:18s} -> 旧写法抛 JSONDecodeError: {e.msg}")

    print()
    print("全部断言通过")


if __name__ == "__main__":
    main()
