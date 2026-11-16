"""探针 3：同步 stream(stream_mode="debug")，验证能否拿到"任务开始"事件。

为什么换这条路：
  astream_events 在 async 图执行下要求 AsyncPostgresSaver，用同步 PostgresSaver 会抛
  NotImplementedError。而 g.stream() 是同步生成器，和 PostgresSaver 天然兼容，
  FastAPI 的 StreamingResponse 也能直接吃同步生成器（Starlette 会放线程池迭代）。

要确认的唯一一件事：debug 模式会不会在节点**开始**时给出事件（而不是只在结束时）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph.build import build_graph  # noqa: E402

NODES = {"intake", "understand", "ask_user", "lookup", "knowledge",
         "plan", "act", "verify", "finalize"}


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "CX-104 从 11月8号开始误判突然变多"
    use_ckpt = "--ckpt" in sys.argv
    g = build_graph(use_checkpoint=use_ckpt)
    cfg = {"configurable": {"thread_id": "probe-debug-1"}} if use_ckpt else {}

    t0 = time.perf_counter()
    prev = t0
    for chunk in g.stream({"user_msg": msg}, cfg, stream_mode="debug"):
        now = time.perf_counter()
        typ = chunk.get("type")
        payload = chunk.get("payload") or {}
        name = payload.get("name", "")
        if typ in ("task", "task_result") and name in NODES:
            gap = now - prev
            prev = now
            print(f"{now-t0:7.2f}s  +{gap:6.2f}s  {typ:11s} {name}")
    print(f"\n完成，总耗时 {time.perf_counter()-t0:.1f}s")


main()
