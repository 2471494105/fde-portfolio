"""跑一条诊断轨迹，打印完整过程（工具调用顺序、结论、校验结果）。

用法：python scripts/run_case.py "用户输入"
不带参数时用 H1 默认输入。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import run  # noqa: E402
from app.graph.nodes import final_to_text  # noqa: E402

DEFAULT = "3号线 CX-101 夜班误判率飙升，白班还好一点，帮我查下原因"


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    print(f"输入：{msg}\n")

    state = run(msg)

    ids = state.get("device_ids") or []
    phen = state.get("phenomenon") or "（未描述）"
    print(f"=== 意图：{state.get('intent', '') or '（无）'} ===")
    print(f"    设备：{', '.join(ids) if ids else '（未指定）'}")
    print(f"    现象：{phen}")
    reason = state.get("intent_reason", "")
    if reason:
        print(f"    依据：{reason}")

    print("\n=== 工具调用轨迹 ===")
    for i, e in enumerate(state.get("evidence", []), 1):
        args = json.dumps(e["args"], ensure_ascii=False)
        res = json.dumps(e["result"], ensure_ascii=False, default=str)
        print(f"{i}. {e['tool']}({args})")
        print(f"   -> {res[:300]}{'...' if len(res) > 300 else ''}")

    print(f"\n=== 步数：{state.get('steps', 0)} ===")
    print("\n=== 结论 ===")
    # final 在 state 里是结构化 dict（序列化推迟到 result_event 出口），
    # 脚本要给人看，走 final_to_text；直接 print(fn) 会打出 dict 的 repr。
    print(final_to_text(state.get("final")))

    blocked = state.get("blocked", [])
    if blocked:
        print(f"\n=== 校验：以下数字未在工具返回中出现 ===\n{blocked}")
    else:
        print("\n=== 校验：结论中的百分比均可溯源 ===")

    # 查无与冲突分开打：前者是"库里确实没有"，后者是"库里有两种说法"。
    # 混成一句"信息不足"，读的人就不知道下一步该换台设备、还是该找人确认口径。
    lookups = state.get("unknown") or []
    if lookups:
        print("\n=== 查无（工具正常返回，但结果为空） ===")
        for u in lookups:
            print(f"- [{u.get('kind')}] {u.get('what')}：{u.get('note') or ''}")
    clashes = state.get("conflicts") or []
    if clashes:
        print("\n=== 文档冲突（同一参数两处取值） ===")
        for c in clashes:
            vals = " / ".join(
                f"{v.get('source')}={v.get('value')}" for v in (c.get("values") or []))
            print(f"- {c.get('param')}：{vals}")


if __name__ == "__main__":
    main()
