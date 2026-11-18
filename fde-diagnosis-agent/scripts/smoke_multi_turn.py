"""多轮冒烟：同一个 thread 连续几轮，验证追问能不能接上。

改造前的问题（实测）：
  1. "帮我看看最近怎么样" → 追问"设备编号 + 想做什么"
  2. 用户照它说的回 "看数据" → 又问"设备编号"
  3. 回 "CX-101" → 又问"你想看哪方面"
  两条路都走不通，因为 ask_user → END 之后 state 就没了。

这个脚本把两件事分开测：
  A. 补设备（第2轮说 CX-101）→ 第3轮说"看数据"应该直接出表
  B. 换话题（诊断完 CX-101 后问另一台）→ 不该沿用上一台的现象
"""

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import run  # noqa: E402
from app.graph.nodes import final_to_text  # noqa: E402


def show(tag, state):
    ids = state.get("device_ids") or []
    print(f"\n--- {tag} ---")
    print(f"意图={state.get('intent')}  轮次={state.get('turn')}  "
          f"设备={ids or '无'}  步数={state.get('steps')}")
    print(f"slots={state.get('slots')}")
    # final 出 state 时是结构化 dict（conclusion/claims/advice），
    # 直接切片会 KeyError: slice(...)——dict 拿 slice 当 key 查。
    print(f"结论：{final_to_text(state.get('final'))[:220].strip()}")


def case_a():
    """零碎描述 → 追问 → 补设备 → 说"看数据"，应该能查出来。"""
    tid = f"smoke-a-{uuid.uuid4().hex[:8]}"
    print("=" * 70)
    print("A. 三轮补全：模糊 → 补设备 → 说看数据")

    s1 = run("帮我看看最近怎么样", thread_id=tid)
    show("第1轮", s1)
    ask1 = final_to_text(s1.get("final"))

    s2 = run("CX-101", thread_id=tid)
    show("第2轮", s2)

    s3 = run("看数据", thread_id=tid)
    show("第3轮", s3)

    # 三处都要 final_to_text：state 里的 final 是结构化 dict。
    # 在 dict 上做 `"设备编号" in final` 查的是 **key**，永远 False ——
    # 断言不会报错，只会一直"通过"，这比崩掉更坏。
    t2, t3 = final_to_text(s2.get("final")), final_to_text(s3.get("final"))
    problems = []
    if "设备编号" in t2:
        problems.append("第2轮又问了设备编号——指令没接上")
    if "设备编号" in t3:
        problems.append("第3轮又问了设备编号——槽位没记住")
    if s3.get("steps", 0) == 0:
        problems.append("第3轮没有查任何数据")
    if "误判率" not in t3:
        problems.append("第3轮没出数据表")

    print("\n【A 判定】", "通过" if not problems else "失败：" + "；".join(problems))
    print(f"（第1轮追问原文首行：{ask1.splitlines()[0] if ask1 else ''}）")
    return problems


def case_b():
    """换设备时不该沿用上一台的现象。"""
    tid = f"smoke-b-{uuid.uuid4().hex[:8]}"
    print("\n" + "=" * 70)
    print("B. 换话题：CX-101 诊断后再问 CX-102 的数据")

    s1 = run("CX-101 1号工位误判特别多，帮我查原因", thread_id=tid)
    show("第1轮", s1)

    s2 = run("那 CX-102 呢", thread_id=tid)
    show("第2轮", s2)

    problems = []
    slots = s2.get("slots") or {}
    if slots.get("device_id") != "CX-102":
        problems.append(f"设备没切到 CX-102（当前 {slots.get('device_id')}）")
    if slots.get("phenomenon"):
        problems.append(f"换设备后仍沿用了旧现象：{slots.get('phenomenon')}")

    print("\n【B 判定】", "通过" if not problems else "失败：" + "；".join(problems))
    return problems


def case_c():
    """同一 thread 的第 3 轮之后，上下文要被裁剪，但摘要要还在。"""
    tid = f"smoke-c-{uuid.uuid4().hex[:8]}"
    print("\n" + "=" * 70)
    print("C. 上下文管理：跑 4 轮，看 messages 长度和摘要")

    for q in ["E-101 是什么意思", "CX-101 误判高吗", "CX-104 呢", "CX-105 也看看"]:
        s = run(q, thread_id=tid, trace_run=False)
        msgs = len(s.get("messages") or [])
        summary = (s.get("session_summary") or "").strip()
        print(f"  轮次={s.get('turn')} messages={msgs} 摘要行数={summary.count(chr(10)) + 1 if summary else 0}")

    print("\n（上下文裁剪在 plan 里对模型生效，不会删存储里的 messages）")


if __name__ == "__main__":
    pa = case_a()
    pb = case_b()
    case_c()
    print("\n" + "=" * 70)
    print("总判定：", "全部通过" if not (pa or pb) else "存在失败")
