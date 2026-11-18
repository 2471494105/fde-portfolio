"""图装配。

START → intake（正则抽候选实体，不判意图）
      → understand（模型判意图，程序只校验自洽性）
          ├─ diagnose  → plan ⇄ act        ┐
          │              plan → check ─────┼→ verify → finalize → END
          ├─ action    → plan ⇄ act        │   （check 不通过就回 plan 一次）
          ├─ lookup    → lookup ───────────┘
          ├─ knowledge → knowledge ─────────┘
          └─ clarify   → ask_user → END

check 是 verify 前面的自查闸口：把"哪条规则、为什么失败"回喂给模型，让它自己改一次。
依据是 Claude Agent SDK 的 gather → act → verify → repeat，verify 在循环内、且产出是失败原因。
回环上限 MAX_CHECK_RETRY=1，"拿 ref=null 换通过"另有一条判定（见 nodes.check）。

多轮靠 checkpointer：同一个 thread_id 再 invoke，会从该 thread 上次的 checkpoint
恢复 state（messages / evidence / slots / session_summary 都在里面）。
"""

from langgraph.graph import END, START, StateGraph

from app import trace
from app.graph.llm import BASE_URL, MODEL, PROVIDER
from app.graph.nodes import (
    act,
    ask_user,
    check,
    final_to_text,
    finalize,
    intake,
    knowledge,
    lookup,
    plan,
    route_after_check,
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
    g.add_node("check", check)
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
        {"act": "act", "check": "check", "finalize": "finalize"},
    )
    # check 是 verify 前面的自查闸口，不是新的终局：
    # 回环一次 → 回 plan 重出结论；不通过但已用掉机会 → 照样交给 verify 判失败。
    # verify 只保留一个出口，判完就是终局，这样"改过几次才通过"和"一次就过"
    # 在报告里能分开看（result_event 的 check_rounds）。
    g.add_conditional_edges(
        "check", route_after_check,
        {"plan": "plan", "verify": "verify"},
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


def result_event(state):
    """result 事件的字段定义，只此一份。

    两个出口都调它：run()（CLI / 评测）和 server._sse（Web 流式）。
    分头写过一次，结果是 _sse 只写 final_head、不写 final 全文，
    于是 Web 跑出来的轨迹**没法离线复算「这条结论为什么被/没被拦」**——
    而同一次运行走 CLI 却可以。同一次运行、两条路径、两份可审计性，
    这种差异不会有任何报错，只会在"想回溯某个线上问题"时才发现证据不全。

    final 必须存全文：blocked 的判定依赖结论全文，只存前 300 字符，
    事后重算拿到的是另一个题目的答案。

    final 在状态里可能是 dict（写入端已解析）也可能是字符串（历史 checkpoint）。
    出口统一序列化成字符串：这里是唯一该做 json.dumps 的地方，
    节点里再 dumps 一遍等于把"结构化"这件事只做了一半。
    """
    final = final_to_text(state.get("final"))
    return {
        "turn": state.get("turn", 1),
        "intent": state.get("intent", ""),
        # 意图判断的自报置信度。原来没有这个字段：判错时没人知道它有多虚。
        # 低于 CONF_ASK 会被降级成追问，所以「意图判错」的统计要从这里分开看——
        # 低置信追问不算判错，那是系统主动交回给用户；低置信照走还走错才是判错。
        "intent_conf": state.get("intent_conf"),
        "steps": state.get("steps", 0),
        # 结束原因必须单列：steps=12 有两种完全不同的含义
        # （budget_exhausted = 被预算掐断的半成品；enough = 12 步内自己判断够了）。
        # 只报步数等于把这两件事合并，事后拿不到任何归因线索。
        "stop_reason": state.get("stop_reason", ""),
        # 回环次数要单列：0 次 = 一次就过，1 次 = 被拦下后改才对。
        # 只报 misquoted 为空，会把这两件事混成"没问题"，
        # 而"改过几次才对"本身就是模型稳定性的一个指标。
        "check_rounds": state.get("check_retry", 0),
        "tools": state.get("tool_names", []),
        "blocked": state.get("blocked", []),
        "misquoted": state.get("misquoted", []),
        "device_ids": state.get("device_ids", []),
        "slots": state.get("slots", {}),
        "final": final,
        "final_head": final[:300],
        # 结论的可读版本：claims 渲染成人话，ref 为空的数字后面带 [无出处]。
        # 前端显示这个，不显示原始 JSON。
        "final_text": state.get("final_text", ""),
        "cite": state.get("cite_stats", {}),
        "cite_fail": state.get("cite_fail", []),
    }


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
        trace.emit("result", thread_id=thread_id, **result_event(state))
        # 把 trace_id 带出去，Web 端据此回查这次运行的完整轨迹
        state["trace_id"] = t.run_id
        state["thread_id"] = thread_id
        return state
