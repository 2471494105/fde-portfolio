"""探针：查无之后，第 2 轮还会不会重复查同一个对象。

背景：
  `finalize` 把 `unknown` 拼进 `session_summary`，下一轮进 plan 的 notes。
  第一版只写了"查无 X"——陈述事实，没给动作。实测第 2 轮模型照旧查了一次。
  改成"已排除：X（原因，别再查）"后重测。

两条对照，性质不同，必须分开看：
  case A  diagnose → lookup
          第 2 轮走 lookup **节点**，那次查询是程序无条件执行的，不是模型决策。
          这一条验的是「结论文案对不对」（C）与「摘要写没写进去」（A 的形态），
          不能拿来判模型会不会重复查。
  case B  diagnose → diagnose
          第 2 轮走 plan→act，**模型有决策权**。这一条才是判据：
          摘要里的"别再查它"到底有没有被当成约束。

判据：
  只看 intent 属于模型决策的轮次（不是 lookup / knowledge）里，
  有没有出现 args.device_id == 目标设备的调用。有就是重复查。

第一版的错误：
  1. 把 lookup 节点程序的调用算成"模型重复查"；
  2. 摘要判据还写着「查无 X」，而措辞已经改成「已排除」。
  两处都让结论失真——判据不跟着改，探针就会一直报同一个假问题。
"""

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import run  # noqa: E402
from app.graph.nodes import final_to_text  # noqa: E402

TARGET = "CX-999"
# 这三种意图的取证是节点里的程序实现，不是模型的工具决策。
PROGRAMMATIC = ("lookup", "knowledge")


def calls_with_device(state, device_id, turn):
    """该轮以目标设备为参数的工具调用。evidence 跨轮累积，必须按 turn 过滤。"""
    hits = []
    for e in state.get("evidence") or []:
        if int(e.get("turn") or 0) != turn:
            continue
        args = e.get("args") or {}
        if str(args.get("device_id") or "") == device_id:
            hits.append(e.get("tool"))
    return hits


def show(tag, state):
    print(f"\n--- {tag} ---")
    print(f"意图={state.get('intent')}  步数={state.get('steps')}  "
          f"工具={state.get('tool_names')}")
    print(f"结论：{final_to_text(state.get('final'))[:220].strip()}")


def case_a():
    """diagnose → lookup：验结论文案（C）与摘要形态（A）。"""
    print("=" * 70)
    print("A. diagnose → lookup（第 2 轮由 lookup 节点执行，不是模型决策）")
    tid = f"probe-a-{uuid.uuid4().hex[:8]}"

    s1 = run(f"{TARGET} 的夜班误判率为什么一直偏高，帮我查下", thread_id=tid)
    show("第1轮", s1)
    summary = (s1.get("session_summary") or "").strip()
    print(f"摘要：{summary}")

    s2 = run(f"那 {TARGET} 最近有工单吗", thread_id=tid)
    show("第2轮", s2)

    # C：claims 为空时不许还说"列出如下"
    concl = (s2.get("final") or {}).get("conclusion", "")
    claims = (s2.get("final") or {}).get("claims", [])
    promised_empty = ("列出如下" in concl) and not claims

    problems = []
    if f"已排除" not in summary or TARGET not in summary:
        problems.append("摘要里没有排除项")
    if promised_empty:
        problems.append(f"claims 为空却仍说「列出如下」：{concl}")
    return problems


def case_b():
    """diagnose → diagnose：第 2 轮模型有决策权，这才是 A 的真正判据。"""
    print("\n" + "=" * 70)
    print("B. diagnose → diagnose（第 2 轮走 plan→act，模型可自主决定查不查）")
    tid = f"probe-b-{uuid.uuid4().hex[:8]}"

    s1 = run(f"{TARGET} 的夜班误判率为什么一直偏高，帮我查下", thread_id=tid)
    show("第1轮", s1)
    summary = (s1.get("session_summary") or "").strip()
    print(f"摘要：{summary}")

    # 措辞必须带"是什么原因"，否则意图会被判成 lookup（只要有设备号+数据诉求就归 lookup），
    # 那一轮又变成节点程序执行，测不到模型的决策。第一次用的"也一样高吗"就掉进了 lookup。
    s2 = run(f"{TARGET} 白班误判率也偏高，是什么原因", thread_id=tid)
    show("第2轮", s2)

    intent = str(s2.get("intent") or "")
    again = calls_with_device(s2, TARGET, turn=2)
    print(f"\n第2轮意图={intent}  "
          f"是否程序节点={intent in PROGRAMMATIC}")
    print(f"第2轮以 {TARGET} 为参数的工具调用：{again or '无'}")

    if intent in PROGRAMMATIC:
        print("（这一轮是节点程序在执行，不能用来判模型是否重复查——换场景）")
        return None
    if again:
        return [f"摘要写了排除项，第 2 轮仍查了 {len(again)} 次（{', '.join(again)}）"]
    return []


if __name__ == "__main__":
    pa = case_a()
    pb = case_b()
    print("\n" + "=" * 70)
    print("【A 判定】", "通过" if not pa else "失败：" + "；".join(pa))
    if pb is None:
        print("【B 判定】无法判定（本轮不是模型决策路径）")
    else:
        print("【B 判定】", "通过（摘要被当成约束）" if not pb else "失败：" + "；".join(pb))
    sys.exit(1 if (pa or pb) else 0)
