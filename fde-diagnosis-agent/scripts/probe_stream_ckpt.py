"""探针 2：astream_events + checkpointer，验证 psycopg 连接跨线程会不会炸。

背景：build_graph(use_checkpoint=True) 用进程内单例 saver，它内部只有一条
psycopg 连接，在主线程创建。astream_events 会把同步节点放到线程池执行，
如果那条连接被工作线程使用，psycopg3 会报线程不安全，多轮会话直接崩。

用法：
    python scripts/probe_stream.py --ckpt        同一 thread_id 连跑两轮
    python scripts/probe_stream.py --ckpt-new    两轮用不同 thread_id 对照
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph.build import build_graph  # noqa: E402

NODES = {"intake", "understand", "ask_user", "lookup", "knowledge",
         "plan", "act", "verify", "finalize"}


async def one(g, msg, thread_id, tag):
    cfg = {"configurable": {"thread_id": thread_id}}
    t0 = time.perf_counter()
    final = ""
    try:
        async for ev in g.astream_events({"user_msg": msg}, cfg, version="v2"):
            if ev["event"] == "on_chain_end" and ev.get("name") == "finalize":
                out = ev.get("data", {}).get("output") or {}
                final = (out.get("final") or "")[:60]
    except Exception as exc:
        print(f"[{tag}] 异常 {type(exc).__name__}: {exc}")
        return False
    print(f"[{tag}] ok  {time.perf_counter()-t0:5.1f}s  final={final!r}")
    return True


async def main():
    use_ckpt = "--ckpt" in sys.argv or "--ckpt-new" in sys.argv
    same_thread = "--ckpt" in sys.argv

    g = build_graph(use_checkpoint=use_ckpt)
    tid = "probe-stream-ckpt" if same_thread else None
    if not tid:
        from app.graph.persistence import new_thread_id
        tid = new_thread_id()

    ok1 = await one(g, "E-101 这个告警什么意思", tid, "轮1")
    if not ok1:
        return
    if same_thread:
        ok2 = await one(g, "E-201 呢", tid, "轮2 同 thread")
    else:
        from app.graph.persistence import new_thread_id
        ok2 = await one(g, "E-201 呢", new_thread_id(), "轮2 新 thread")
    print("\n结论：", "两轮都通过" if ok2 else "第二轮失败")


asyncio.run(main())
