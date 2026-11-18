"""验证跨轮摘要真的被上限管住，且窗口与注入口径一致。

为什么这条值得单独一个脚本：
  "第 20 轮时摘要只留 8 行"是个跨轮性质，任何单轮测试都看不见。
  而它错了的后果又是隐形的——上下文里多出十几轮摘要，模型照样能回答，
  只是每一轮都在为很久以前付 token，报告上却看不出异常。

为什么不用调模型：
  finalize 是规则拼的（不调模型），reducer 是纯函数。整条链里没有模型，
  所以跑真函数比端点对端更准、更快、还能断言细节。

reducer 特意从注解上取（get_type_hints 的 __metadata__），不手写 _tail_lines：
  如果哪天改了函数忘了改 State 上的 Annotated，手写版照样绿，取注解版会红。
"""

import sys
from pathlib import Path
from typing import get_type_hints

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph import nodes  # noqa: E402
from app.graph import state as gs  # noqa: E402

TURNS = 20


def _reducer():
    hints = get_type_hints(gs.State, include_extras=True)
    return hints["session_summary"].__metadata__[0]


def accumulate():
    """跑真实 finalize TURNS 次，每轮把产出过 reducer。"""
    print("=" * 70)
    print(f"[1] 跨轮累积（{TURNS} 轮，cap={gs.SESSION_SUMMARY_KEEP}）")
    red = _reducer()
    summary = ""
    rows = []
    for turn in range(1, TURNS + 1):
        out = nodes.finalize({
            "turn": turn,
            "intent": "diagnose",
            "tool_names": ["query_device_profile", "query_defect_log",
                           "query_work_order"],
            "user_msg": f"第{turn}轮：CX-104 的 1 号工位误判率一直偏高，帮我查一下",
            "final": {"conclusion": "误判集中在夜间班次，建议先核对照参数",
                      "claims": [], "advice": []},
            "stop_reason": "",
        })
        summary = red(summary, out.get("session_summary", ""))
        rows.append((turn, len(summary.splitlines()), len(summary)))

    for turn, lines, chars in rows:
        flag = "  <- 触顶" if lines == gs.SESSION_SUMMARY_KEEP and turn <= gs.SESSION_SUMMARY_KEEP else ""
        print(f"  轮 {turn:>2}: {lines} 行, {chars} 字符{flag}")
    return summary, rows


def check_stable(rows):
    """上限之后必须恒定：行数不变、字符只随轮号位数微变。"""
    print("=" * 70)
    print("[2] 上限之后的稳定性")
    cap = gs.SESSION_SUMMARY_KEEP
    over = [(t, l, c) for t, l, c in rows if t > cap]
    lines = {l for _, l, _ in over}
    chars = [c for _, _, c in over]
    print(f"  cap 之后行数集合: {lines}（必须只有一个值，且等于 {cap}）")
    print(f"  cap 之后字符范围: {min(chars)} ~ {max(chars)}")
    if len(over) >= 2:
        ratio = chars[-1] / chars[0]
        print(f"  末轮/首个触顶轮: {ratio:.2f}（接近 1 = 不随轮数线性增长）")
    ok = lines == {cap}
    print(f"  结论: {'PASS' if ok else 'FAIL'}")
    return ok


def check_injection(summary):
    """注入端：模型实际看到的那一段。"""
    print("=" * 70)
    print("[3] 注入端口径")
    notes, _, _ = nodes._plan_notes(
        {"turn": TURNS, "session_summary": summary, "missing": [], "steps": 9},
        used=[],
    )
    hit = [n for n in notes if "前几轮发生了什么" in n]
    if hit:
        block = hit[0]
        print(f"  _plan_notes 注入 {len(block)} 字符")
        print(f"  覆盖从: {block.splitlines()[1][:24] if len(block.splitlines()) > 1 else '?'}")
    else:
        print("  FAIL: 摘要没有进 prompt")
    return bool(hit)


def check_window():
    """窗口：只剩最近 N 轮 + 首句原始诉求。"""
    print("=" * 70)
    print("[4] 上下文窗口（第 20 轮）")
    msgs = [{"role": "user", "content": "第一句原始诉求"}]
    for t in range(1, TURNS + 1):
        msgs.append({"role": "user", "content": f"第{t}轮提问"})
        msgs.append({"role": "assistant", "content": f"第{t}轮结论"})
    kept = nodes._context_messages({"messages": msgs, "evidence": [],
                                   "turn": TURNS})
    users = [m.get("content") for m in kept if m.get("role") == "user"]
    print(f"  原始 {len(msgs)} 条 → 保留 {len(kept)} 条")
    print(f"  user 消息: {users}")
    ok = users and users[0] == "第一句原始诉求" and len(users) == nodes.CTX_VISIBLE_TURNS + 1
    print(f"  结论: {'PASS' if ok else 'FAIL'}"
          f"（首句保留，且只留最近 {nodes.CTX_VISIBLE_TURNS} 轮）")
    return ok


def main():
    summary, rows = accumulate()
    results = [
        check_stable(rows),
        check_injection(summary),
        check_window(),
    ]
    print("=" * 70)
    print(f"总计: {'PASS' if all(results) else 'FAIL'}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
