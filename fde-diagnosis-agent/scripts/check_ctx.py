"""验证两件事：checkpoint 真落 PG、上下文裁剪真生效。

为什么要在真实入口验证而不是读代码推理：
  裁剪函数写对了，不代表 plan 真的调用它；checkpointer 编译进图了，
  不代表表里真有行。两种"看起来对"都过不了面试追问。
"""

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import run  # noqa: E402
from app.graph.nodes import MAX_CTX_TURNS, _context_messages  # noqa: E402
from app.graph.persistence import DSN, get_saver  # noqa: E402


def check_cut_unit():
    """单元级：伪造 6 轮 messages，看裁完还剩什么。"""
    print("=" * 70)
    print("[1] 裁剪函数单测")

    msgs = [{"role": "user", "content": "第一句原始诉求"}]
    for t in range(1, 7):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{t}", "type": "function",
             "function": {"name": "query_device_profile", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{t}", "content": f"第{t}轮工具返回"})
        msgs.append({"role": "assistant", "content": f"第{t}轮结论"})
        if t < 6:
            msgs.append({"role": "user", "content": f"第{t+1}轮提问"})

    out = _context_messages({"messages": msgs})
    print(f"  原始 {len(msgs)} 条 → 裁剪后 {len(out)} 条（MAX_CTX_TURNS={MAX_CTX_TURNS}）")
    print(f"  首条：{out[0].get('content')}")
    print(f"  保留的 user 提问：{[m.get('content') for m in out if m.get('role') == 'user']}")

    # 关键不变量：任何 tool 消息都必须有配对的 tool_calls，否则 OpenAI 直接报错
    ok_pair = True
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            j = i - 1
            while j >= 0 and out[j].get("role") == "tool":
                j -= 1
            if j < 0 or not (out[j].get("role") == "assistant" and out[j].get("tool_calls")):
                ok_pair = False
    print(f"  tool 消息与 tool_calls 配对完整：{ok_pair}")
    if out[0].get("content") != "第一句原始诉求":
        ok_pair = False
        print("  !! 首句诉求丢了")
    return ok_pair


def check_checkpoint_pg():
    """真实入口：同一 thread 跑 3 轮，然后直接查 PG 的 checkpoints 表。"""
    print("\n" + "=" * 70)
    print("[2] checkpoint 落库")

    tid = f"verify-{uuid.uuid4().hex[:10]}"
    qs = ["CX-101 1号工位误判多，查原因", "看数据", "那 CX-104 呢"]
    for i, q in enumerate(qs, 1):
        s = run(q, thread_id=tid, trace_run=False)
        full = len(s.get("messages") or [])
        cut = len(_context_messages(s))
        print(f"  第{i}轮 turn={s.get('turn')} 意图={s.get('intent')} "
              f"messages 全量={full} 送模型={cut}")

    saver = get_saver()
    conn = saver.conn
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from checkpoints where thread_id = %s", (tid,))
        row = cur.fetchone()
        # PostgresSaver 自己设了 dict_row 的 row factory，只能按键取
        n = row["n"] if isinstance(row, dict) else row[0]
    print(f"  PG checkpoints 表中该 thread 的行数：{n}")
    print(f"  thread_id：{tid}")
    return n >= len(qs)


def check_restore_after_new_process():
    """跨进程恢复：新进程用同一 thread 再 invoke，应该接着上一轮。"""
    print("\n" + "=" * 70)
    print("[3] 提示：跨进程恢复由本脚本的第二次运行验证（见下方说明）")
    tid_file = ROOT / "runs" / "last_thread.txt"
    if tid_file.exists():
        tid = tid_file.read_text(encoding="utf-8").strip()
        s = run("看数据", thread_id=tid, trace_run=False)
        print(f"  复用了上一进程留下的 thread：{tid}")
        print(f"  恢复后 turn={s.get('turn')}（说明上一轮的 turn 被读回来了）")
        print(f"  slots={s.get('slots')}")
        tid_file.unlink()
        return int(s.get("turn") or 0) >= 2
    return None


if __name__ == "__main__":
    a = check_cut_unit()
    b = check_checkpoint_pg()
    c = check_restore_after_new_process()
    print("\n" + "=" * 70)
    print(f"裁剪={a}  落库={b}  跨进程恢复={c}")
    tid_file = ROOT / "runs" / "last_thread.txt"
    if c is None and b:
        # 留个 thread 给下一次运行做跨进程验证
        from app.graph.persistence import new_thread_id
        tid = new_thread_id()
        run("CX-102 批次误判多，查原因", thread_id=tid, trace_run=False)
        tid_file.write_text(tid, encoding="utf-8")
        print(f"已留 thread {tid}，再跑一次本脚本即可验证跨进程恢复")
