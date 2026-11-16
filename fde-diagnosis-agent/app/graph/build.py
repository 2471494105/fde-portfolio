"""图装配。

START → intake（正则抽候选实体，不判意图）
      → understand（模型判意图，程序只校验自洽性）
          ├─ diagnose  → plan ⇄ act → verify → finalize → END
          ├─ action    → plan ⇄ act → verify → finalize → END
          ├─ lookup    → lookup ────────────┘
          ├─ knowledge → knowledge ─────────┘
          └─ clarify   → ask_user → END

多轮靠 checkpointer：同一个 thread_id 再 invoke，会从该 thread 上次的 checkpoint
恢复 state（messages / evidence / slots / session_summary 都在里面）。
"""

from langgraph.graph import END, START, StateGraph

from app import trace
from app.graph.llm import BASE_URL, MODEL, PROVIDER
from app.graph.nodes import (
    act,
    ask_user,
    finalize,
    intake,
    knowledge,
    lookup,
    plan,
    route_after_plan,
    route_after_understand,
    understand,
    verify,
)
from app.graph.persistence import get_saver, new_thread_id
from app.graph.state import State


def build_graph(use_checkpoint: bool = True):
    g = StateGraph(State)
    g.add_node("intake", intake)
    g.add_node("understand", understand)
    g.add_node("ask_user", ask_user)
    g.add_node("lookup", lookup)
    g.add_node("knowledge", knowledge)
    g.add_node("plan", plan)
    g.add_node("act", act)
    g.add_node("verify", verify)
    g.add_node("finalize", finalize)

    g.add_edge(START, "intake")
    g.add_edge("intake", "understand")
    g.add_conditional_edges(
        "understand", route_after_understand,
        {"plan": "plan", "lookup": "lookup", "knowledge": "knowledge", "ask_user": "ask_user"},
    )
    g.add_conditional_edges(
        "plan", route_after_plan,
        {"act": "act", "verify": "verify", "finalize": "finalize"},
    )
    g.add_edge("act", "plan")
    g.add_edge("lookup", "verify")
    g.add_edge("knowledge", "verify")
    g.add_edge("verify", "finalize")
    g.add_edge("finalize", END)
    g.add_edge("ask_user", END)

    if use_checkpoint:
        return g.compile(checkpointer=get_saver())
    return g.compile()


def run(user_msg: str, thread_id: str | None = None, trace_run: bool = True,
        use_checkpoint: bool = True) -> dict:
    """跑一轮。

    thread_id 传同一个值就是同一场会话：上一轮追问的内容、已确认的设备，
    都由 checkpointer 带回来。传 None 则开一次性会话（评测、单次 CLI 用）。

    use_checkpoint=False 用于**并发评测**：checkpointer 是进程内单例且只有一条
    psycopg 连接，多线程同时 put 会报 "another command is already in progress"。
    并发场景下每条用独立、无持久化的图，代价是失去多轮能力（评测本来就单轮）。
    """
    app = build_graph(use_checkpoint=use_checkpoint)
    thread_id = thread_id or new_thread_id()
    config = {"configurable": {"thread_id": thread_id}}

    # 首轮 state 为空，messages 由 intake 补；之后每轮只传新的 user_msg，
    # 其余字段从 checkpoint 恢复
    init = {"user_msg": user_msg}

    if not trace_run:
        state = app.invoke(init, config)
        state["thread_id"] = thread_id
        return state

    with trace.Tracer(user_msg, model=MODEL, provider=PROVIDER, base_url=BASE_URL) as t:
        state = app.invoke(init, config)
        trace.emit(
            "result",
            thread_id=thread_id,
            turn=state.get("turn", 1),
            intent=state.get("intent", ""),
            steps=state.get("steps", 0),
            tools=state.get("tool_names", []),
            blocked=state.get("blocked", []),
            misquoted=state.get("misquoted", []),
            device_ids=state.get("device_ids", []),
            slots=state.get("slots", {}),
            # final 存全文，不只 head：判定（blocked）依赖结论全文，
            # 只存前 300 字符会导致事后无法离线复算"这条为什么被/没被拦"
            final=state.get("final", ""),
            final_head=state.get("final", "")[:300],
            # 结论的可读版本：claims 渲染成人话，ref 为空的数字后面带[无出处]。
            # 前端显示这个，不显示原始 JSON。
            final_text=state.get("final_text", ""),
        )
        # 把 trace_id 带出去，Web 端据此回查这次运行的完整轨迹
        state["trace_id"] = t.run_id
        state["thread_id"] = thread_id
        return state
