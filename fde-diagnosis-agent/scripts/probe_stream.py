"""探针：验证 LangGraph astream_events 能否给出节点级开始/结束事件。

目的只有一个——确认「plan 正在跑」这件事能不能在 LLM 返回之前就被观察到。
如果不能，流式输出只能做到"事后逐条播报"，观感几乎没改善。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph.build import build_graph  # noqa: E402

NODES = {"intake", "understand", "ask_user", "lookup", "knowledge",
         "plan", "act", "verify", "finalize"}


async def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "E-101 这个告警什么意思"
    g = build_graph(use_checkpoint=False)
    t0 = time.perf_counter()
    prev = t0
    seq = 0
    seen_names = set()

    async for ev in g.astream_events({"user_msg": msg}, version="v2"):
        e = ev["event"]
        n = ev.get("name", "")
        seen_names.add((e, n))
        if e in ("on_chain_start", "on_chain_end") and n in NODES:
            now = time.perf_counter()
            gap = now - prev
            prev = now
            seq += 1
            print(f"{now - t0:7.2f}s  +{gap:6.2f}s  {seq:>2}  {e:14s} {n}")

    print("\n--- 全部 (event, name) 组合 ---")
    for pair in sorted(seen_names):
        print("   ", pair)


asyncio.run(main())
