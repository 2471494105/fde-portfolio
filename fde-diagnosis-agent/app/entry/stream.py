"""平台无关的事件流：把一次 LangGraph 运行投影成帧序列。

从 app/web/server.py 抽出来（DOC-37 W1）：
  Web -> SSE 字符串（server._sse 只做编码）
  IM  -> 卡片更新（app/entry/feishu/render.py）

帧是平台中立的，所以"两个入口同一判定"在展示层也成立——连进度文案都是同一份。
"""

import time

from app import trace
from app.entry.labels import TOOL_CN, _brief, cite_text, intent_cn, tool_cn
from app.graph.build import build_graph, result_event
from app.graph.llm import BASE_URL, MODEL, PROVIDER
from app.graph.persistence import new_thread_id

# ============ 流式诊断 ============
#
# 为什么用同步 stream(stream_mode="debug") 而不是 astream_events：
#   astream_events 在 async 图执行下要求 AsyncPostgresSaver，用同步 PostgresSaver 会抛
#   NotImplementedError（已实测，scripts/probe_stream_ckpt.py 可复现）。
#   而 g.stream() 是同步生成器，和 PostgresSaver 天然兼容；StreamingResponse 也能直接吃
#   同步生成器（Starlette 会把它丢进线程池迭代）。
#
# 为什么用 debug 而不是 updates：
#   updates 只在节点**结束**后给结果。模型那十几秒里前端一片空白，等于没流式。
#   debug 的 task 事件在节点**开始**时发出——这才是"看得见的进展"的来源。
#
# 实测依据（scripts/probe_stream_debug.py）：
#   understand 开始 0.03s / 结束 5.18s；plan 开始 5.18s / 结束 10.13s
#   一条 8 步诊断里，plan 累计 30.2s（83%），act 累计 0.73s（2%）。

STREAM_NODES = {"understand", "plan", "act", "verify", "finalize",
                "knowledge", "lookup", "ask_user"}

# 节点名 → 用户看得懂的话。内部术语（plan/act/verify）不直接暴露给现场人员。
NODE_LABEL = {
    "understand": "理解你的问题",
    "plan": "判断下一步查什么",
    "act": "执行查询",
    "verify": "核对结论依据",
    "finalize": "整理结论",
    "knowledge": "检索设备手册",
    "lookup": "读取设备档案",
    "ask_user": "整理追问",
}


def _node_detail(name: str, delta) -> str:
    """把一个节点的 state 增量翻译成一句人话。"""
    d = delta if isinstance(delta, dict) else {}
    if name == "understand":
        # 意图也要说人话：action 对现场没有意义，写"执行动作（建单 / 派工）"才对得上他要做的事
        return f"意图：{intent_cn(d.get('intent')) or d.get('intent') or '-'}"
    if name == "plan":
        calls = []
        for m in (d.get("messages") or []):
            for tc in (m.get("tool_calls") or []):
                calls.append(tc["function"]["name"])
        return ("决定调用 " + "、".join(tool_cn(c) for c in calls)) if calls else "准备收尾"
    if name == "act":
        names = [m["name"] for m in (d.get("messages") or []) if m.get("name")]
        return ("、".join(tool_cn(n) for n in names) + " 已返回") if names else "无工具返回"
    if name == "verify":
        blocked = d.get("blocked") or []
        quoted = d.get("misquoted") or []
        s = d.get("cite_stats") or {}
        # 伪引用和"没标出处"要分开显示：前者是错，后者只是没写。
        if quoted:
            return "引用写错 " + "、".join(quoted)
        if blocked:
            return "未溯源 " + "、".join(blocked)
        if s.get("json_bad"):
            return "结论未按 claims 契约输出，引用无法核验"
        if s.get("qty"):
            parts = [f"数字 {s['qty']} 个", f"有出处 {s.get('cited_ok', 0)}"]
            if s.get("unsourced"):
                parts.append(f"无出处 {s['unsourced']}（已标灰）")
            return " · ".join(parts)
        return "结论里没有需要溯源的数字"
    if name == "finalize":
        return f"结论 {len(d.get('final') or '')} 字"
    if name in ("knowledge", "lookup", "ask_user"):
        return f"引用 {len(d.get('evidence') or [])} 条数据"
    return ""


def _done_event(state, *, trace_id, thread_id, elapsed_ms):
    """前端收尾帧的字段。

    公共部分从 result_event 取，不手抄——两个出口共用一份定义是它存在的理由，
    而 done 帧原来又抄了一遍，重合 9 个字段，改一个漏一个不报错。

    抽成独立函数是为了能被单测直接调：写在 _sse 里的话，只能靠读源码断言
    "包含某个字符串"，那种断言在重构后照样绿。

    私有部分只有这个实时帧需要：evidence 给 UI 画证据列表，trace_id 给"查轨迹"
    的按钮，total_ms 给耗时。
    """
    base = result_event(state)
    return {
        "type": "done",
        **base,
        # 意图的中文写法单独给一栏，不覆盖原值：审计仍然要看到 intent 枚举本身
        "intent_cn": intent_cn(base.get("intent")),
        # 结论正文里的引用标签形如 [query_defect_log{group_by=...}]，前端拿这份映射
        # 只把工具名换成中文，参数原样保留——改了就没法和 runs/*.jsonl 对账
        "tool_cn_map": TOOL_CN,
        # 翻好的结论正文（只动 [工具{参数}] 这种标签，正文其余一字不改）。
        # 取的是 final_text，不是 final——final 是 result_event 给的 JSON 串，
        # 拿去渲染会把整段 {"conclusion":…} 铺到页面上（实测踩过）。
        # 原 final_text / final 都原样返回，审计看的还是模型写的那份
        "final_cn": cite_text(base.get("final_text") or ""),
        "evidence": _brief(state.get("evidence", []), state.get("steps", 0)),
        "trace_id": trace_id,
        "thread_id": thread_id,
        "total_ms": elapsed_ms,
    }


def iter_events(msg: str, thread_id: str | None = None):
    """把一次运行投影成帧：open / node_start / node_end / done / error。

    平台无关——Web 把帧序列化成 SSE，IM 把帧渲染成卡片。
    每轮 next() 之前必须重新 activate()：Starlette 的线程池与飞书回调线程都是
    每次取值换一个 context，只在开头 activate 一次的话，第一个 yield 之后就失效，
    节点里的 add_tokens / trace.emit 会被静默丢弃（jsonl 里 token 恒为 0）。
    """
    g = build_graph(use_checkpoint=True)
    tid = thread_id or new_thread_id()
    cfg = {"configurable": {"thread_id": tid}}

    t = trace.Tracer(msg, model=MODEL, provider=PROVIDER, base_url=BASE_URL)
    t.activate()
    t0 = time.perf_counter()
    started: dict[str, float] = {}
    err: str | None = None

    try:
        yield {"type": "open", "thread_id": tid}

        it = g.stream({"user_msg": msg}, cfg, stream_mode="debug")
        while True:
            t.activate()
            try:
                chunk = next(it)
            except StopIteration:
                break
            typ = chunk.get("type")
            pl = chunk.get("payload") or {}
            name = pl.get("name")
            if name not in STREAM_NODES:
                continue

            if pl.get("error"):
                err = str(pl["error"])
                yield {"type": "error", "message": err}
                continue

            now = round((time.perf_counter() - t0) * 1000)
            if typ == "task":
                started[name] = time.perf_counter()
                yield {"type": "node_start", "node": name,
                       "label": NODE_LABEL.get(name, name), "t": now}
                t.write("node_start", node=name)
            elif typ == "task_result":
                ms = round((time.perf_counter() - started.get(name, time.perf_counter())) * 1000)
                detail = _node_detail(name, pl.get("result"))
                yield {"type": "node_end", "node": name,
                       "label": NODE_LABEL.get(name, name),
                       "ms": ms, "detail": detail, "t": now}
                t.write("node_end", node=name, ms=ms, detail=detail)

        state = (g.get_state(cfg).values or {})
        done = _done_event(state, trace_id=t.run_id, thread_id=tid,
                           elapsed_ms=round((time.perf_counter() - t0) * 1000))
        yield done
        t.write("result", thread_id=tid, **result_event(state))
    except Exception as exc:  # 生成器里的异常必须变成一帧，否则前端只看到连接断
        err = f"{type(exc).__name__}: {exc}"
        t.write("stream_error", error=err)
        yield {"type": "error", "message": err}
    finally:
        t.deactivate()
        try:
            t.close(error=err)
        except Exception:
            pass