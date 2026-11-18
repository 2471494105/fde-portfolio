"""导出 State 每个字段的真实取值样本。

为什么要有这个脚本：
  《State 字段分工表》里"类型"一列写的是 `list` / `dict`。这些词回答不了
  "里面装什么"。`unknown` 是 `list`，但真正要写代码的人需要知道元素有
  `kind` / `what` / `note` 三个键、`note` 是陈述句不是原因码。
  这类信息手写示例不可信——手写的一定是"我以为它长什么样"。

所以跑真实会话，把每轮结束时的完整 state 导出来，按字段取代表值。

四个 case 走不同路径，因为不同分支写的字段不同：
  1 diagnose  → understand / plan / act / check / verify / finalize
  2 查无      → 同上，但工具走查无分支，填 unknown
  3 knowledge → 不走 plan/act，填 conflicts / svc_fail / manual_empty
  4 缺槽      → understand 判 clarify，填 missing / last_question / intent_note

产物：docs/state_samples_20261118.json
  - 每个字段一段真实的取值，长文本按 LIMIT 截断并标记
  - 取不到的字段（如本轮路径没经过 check）明确标 "absent"，不补假值
"""

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import run  # noqa: E402
from app.graph.state import RESET_EACH_TURN  # noqa: E402

LIMIT = 220  # 单个字符串字段截断长度

CASES = [
    # 措辞是"为什么一直偏高、帮我查下"——这是之前探针里唯一稳定判成 diagnose 的说法。
    # 换成"帮我查下原因"会被判成 lookup（有设备号 + 数据诉求就归 lookup），
    # 那样 plan/act/check 整条路径都不执行，check_* / stop_reason 只能取到空值。
    ("diagnose_ok", "CX-101 的夜班误判率为什么一直偏高，帮我查下"),
    ("diagnose_unknown", "CX-999 的夜班误判率为什么一直偏高"),
    ("knowledge", "标定周期是多少天"),
    ("clarify", "夜班误判率偏高怎么办"),
]


def cut(s):
    s = str(s)
    return s if len(s) <= LIMIT else s[:LIMIT] + f"…(+{len(s) - LIMIT})"


def norm(v, depth=0):
    """压成可 JSON 的形式，同时保留"这是什么类型、里面装什么"。"""
    if depth > 3:
        return cut(v)
    # LangChain 消息对象：只留类型和前若干字，原文进 messages 是累积字段，不需要全文
    if hasattr(v, "content") and hasattr(v, "type"):
        return {"_type": type(v).__name__, "role": v.type,
                "content": cut(getattr(v, "content", ""))}
    if isinstance(v, dict):
        return {str(k): norm(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [norm(x, depth + 1) for x in v[:3]]
    if isinstance(v, str):
        return cut(v)
    return v


def shape(v):
    """给字段贴一个"结构摘要"，用来填表时快速看。"""
    if isinstance(v, dict):
        return "dict{" + ", ".join(sorted(map(str, v.keys())))[:120] + "}"
    if isinstance(v, list):
        first = v[0] if v else None
        if isinstance(first, dict):
            return "list[" + ", ".join(sorted(map(str, first.keys())))[:120] + "]"
        return f"list[{type(first).__name__}]" if v else "list[]"
    return type(v).__name__


def main():
    out = {"generated_for": "State 字段分工表 的数据结构列",
           "cases": {}, "fields": {}}

    for name, q in CASES:
        tid = f"state-sample-{uuid.uuid4().hex[:8]}"
        print(f"\n=== case {name}: {q}")
        state = run(q, thread_id=tid)
        rec = {"query": q, "thread": tid,
               "intent": state.get("intent"),
               "steps": state.get("steps"),
               "stop_reason": state.get("stop_reason"),
               "tool_names": state.get("tool_names"),
               "state": {k: norm(v) for k, v in sorted(state.items())}}
        out["cases"][name] = rec
        print(f"  intent={rec['intent']} steps={rec['steps']} "
              f"stop={rec['stop_reason']} tools={rec['tool_names']}"
              f" fields={len(state)}")

    # 汇总：每个字段在哪些 case 里有值、结构长什么样、取哪个 case 的样本
    keys = set()
    for rec in out["cases"].values():
        keys |= set(rec["state"].keys())
    for k in sorted(keys):
        present = [c for c, r in out["cases"].items() if k in r["state"]]
        # 优先取"非空"的那个 case，空值样本没有说明力
        sample_case, sample_val = None, None
        for c in present:
            v = out["cases"][c]["state"][k]
            if v not in ([], {}, "", None, 0):
                sample_case, sample_val = c, v
                break
        if sample_case is None and present:
            sample_case, sample_val = present[0], out["cases"][present[0]]["state"][k]
        out["fields"][k] = {
            "shape": shape(sample_val),
            "in_reset": k in RESET_EACH_TURN,
            "seen_in": present,
            "sample_case": sample_case,
            "sample": sample_val,
        }

    path = ROOT / "docs" / "state_samples_20261118.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {path}")

    print("\n=== 字段总览 ===")
    for k, f in out["fields"].items():
        flag = "" if f["sample_case"] else "  << 四个 case 都没取到"
        print(f"{k:18s} reset={str(f['in_reset']):5s} {f['shape'][:70]:70s} <- {f['sample_case']}{flag}")


if __name__ == "__main__":
    main()
