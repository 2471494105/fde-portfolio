"""会话持久化：把图状态存进 PostgreSQL。

为什么把 state 交给 checkpointer，而不是自己建一张 sessions 表：
1. 状态本身就是 LangGraph 的 State，自己再维护一份必然和它不同步
2. checkpointer 顺带把"进程挂了重启还能接着跑"补上了——同一个 thread_id 重新
   invoke，会从这个 thread 最后一次 checkpoint 恢复，不用重跑前面几轮

thread_id 就是会话 id。Web 端由前端生成并存 localStorage，CLI 用 --thread 指定。
"""

import os
import uuid

from langgraph.checkpoint.postgres import PostgresSaver

DSN = os.getenv("FDE_PG_DSN", "postgresql://postgres:postgres@localhost:5432/fde_rag")

_saver = None
_cm = None


def get_saver() -> PostgresSaver:
    """进程内单例。

    from_conn_string 是个 context manager，这里手动 enter 并保持到进程结束：
    Web 服务是长驻的，每轮都新建连接会很快把 PG 的连接数打满。
    """
    global _saver, _cm
    if _saver is None:
        _cm = PostgresSaver.from_conn_string(DSN)
        _saver = _cm.__enter__()
        _saver.setup()          # 建表，可重复调用（内部是 IF NOT EXISTS）
    return _saver


def close():
    global _saver, _cm
    if _cm is not None:
        try:
            _cm.__exit__(None, None, None)
        except Exception:
            pass
    _saver, _cm = None, None


def new_thread_id() -> str:
    """一次性会话（评测、单次 CLI 用）。用完就没必要再读了。"""
    return f"tmp-{uuid.uuid4().hex[:12]}"
