"""Web 界面：FastAPI + 单页 HTML，无前端框架、无外部 CDN，断网也能演示。

三个接口：
- POST /api/diagnose  跑一轮，返回结论 + 轨迹摘要 + thread_id
- GET  /api/runs      列出历史轨迹
- GET  /api/runs/{id} 取某次运行的完整事件序列（回放用）

多轮怎么走：前端在 localStorage 里存一个 session_id，每次请求带上，
后端原样当 thread_id 传给图。没有它就开一次性会话——所以刷新页面不丢会话，
点"新会话"只是换一个 uuid。
"""

import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import trace
from app.graph.build import build_graph, run
from app.graph.llm import BASE_URL, MODEL, PROVIDER
from app.graph.persistence import new_thread_id

STATIC = Path(__file__).resolve().parent / "static"

# 只允许这两类标识符，顺便挡掉 ../../ 这类路径拼接
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

app = FastAPI(title="视觉检测设备故障诊断 Agent", version="1.0")


class DiagnoseIn(BaseModel):
    msg: str
    session_id: str | None = None


def _as_obj(res):
    """工具返回值类型不统一：多数是 dict，但 query_device_profile 这类返回的是
    已经序列化好的 JSON 字符串。若直接 json.dumps 就会套上第二层引号，前端
    JSON.parse 得到的是 str 而不是 dict，摘要逻辑全部失效（表现为界面显示原始
    JSON 片段）。这里统一归一化，让下游只面对对象。"""
    if isinstance(res, str):
        s = res.strip()
        if s[:1] in "{[":
            try:
                return json.loads(s)
            except Exception:
                return res
    return res


def _summarize(res):
    """把工具返回压成一句人话。

    为什么不交给前端解析：前端拿到的是 json.dumps(res)[:600]，600 字一截就不是合法
    JSON 了，parse 必然失败，于是每条都只能显示"太长"。摘要必须在对象还被完整持有的
    这一侧算出来。
    """
    res = _as_obj(res)
    if isinstance(res, dict) and res.get("error"):
        return "错误：" + str(res["error"])
    if isinstance(res, list):
        return f"{len(res)} 条记录"
    if isinstance(res, dict):
        parts = []
        lists = [(k, v) for k, v in res.items() if isinstance(v, (list, tuple))]
        if lists:
            k, v = max(lists, key=lambda kv: len(kv[1]))
            parts.append(f"{k} {len(v)} 条")
            # 报字段名没用，现场人员想知道的是"第一条长什么样"
            if v and isinstance(v[0], dict):
                k0, v0 = list(v[0].items())[0]
                # 首条的值可能是一整段手册正文，必须截
                s0 = v0[:24] + "…" if isinstance(v0, str) and len(v0) > 24 else v0
                parts.append(f"首条 {k0}={s0}")
        for k, v in res.items():
            if isinstance(v, (int, float, bool)):
                parts.append(f"{k}={v}")
            elif isinstance(v, str) and len(v) <= 40:
                parts.append(f"{k}={v}")
            if len(parts) >= 3:
                break
        return "　".join(parts) if parts else "空结果"
    if isinstance(res, str):
        return res[:45] + ("…" if len(res) > 45 else "")
    return str(res)[:45]


def _brief(evidence, steps=0):
    """给前端用的证据摘要：只留判定需要的字段，不把全量返回塞进页面。

    多轮会话里 evidence 是累积的：第二轮可能只查 1 次，evidence 却有 11 条。
    界面如果只显示"工具调用 1 次"却给出 10 个数字，用户会误判这些数字是刚查的。
    所以按 steps 切出本轮新增的那几条，给每条打 current 标记，前端据此分层展示。
    """
    out = []
    cur = evidence[-steps:] if steps > 0 else []
    cur_ids = {id(x) for x in cur}
    for e in evidence:
        res = _as_obj(e.get("result"))
        ok = not (isinstance(res, dict) and "error" in res)
        out.append({
            "tool": e["tool"],
            "args": e["args"],
            "ok": ok,
            "error": (res.get("error") if not ok else None),
            "summary": _summarize(res),
            "head": json.dumps(res, ensure_ascii=False, default=str)[:600],
            "current": id(e) in cur_ids,
        })
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")


def _validate(req: DiagnoseIn) -> str | None:
    """输入校验，两个接口共用——流式版单独写一份迟早会和同步版走偏。"""
    if not req.msg.strip():
        raise HTTPException(400, "输入不能为空")
    if len(req.msg) > 2000:
        raise HTTPException(400, "输入过长（上限 2000 字）")
    sid = (req.session_id or "").strip() or None
    if sid and not _ID_RE.match(sid):
        raise HTTPException(400, "session_id 不合法")
    return sid


@app.post("/api/diagnose")
def diagnose(req: DiagnoseIn):
    sid = _validate(req)
    state = run(req.msg, thread_id=sid)
    return {
        "final": state.get("final", ""),
        "final_text": state.get("final_text", ""),
        "blocked": state.get("blocked", []),
        "misquoted": state.get("misquoted", []),
        "steps": state.get("steps", 0),
        "tools": state.get("tool_names", []),
        "evidence": _brief(state.get("evidence", [])),
        "trace_id": state.get("trace_id", ""),
        "thread_id": state.get("thread_id", ""),
        "turn": state.get("turn", 1),
        "intent": state.get("intent", ""),
        "slots": state.get("slots", {}),
    }


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
        return f"意图：{d.get('intent') or '-'}"
    if name == "plan":
        calls = []
        for m in (d.get("messages") or []):
            for tc in (m.get("tool_calls") or []):
                calls.append(tc["function"]["name"])
        return ("决定调用 " + "、".join(calls)) if calls else "准备收尾"
    if name == "act":
        names = [m["name"] for m in (d.get("messages") or []) if m.get("name")]
        return ("、".join(names) + " 已返回") if names else "无工具返回"
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


def _frame(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False, default=str) + "\n\n"


def _sse(msg: str, sid: str | None):
    """同步生成器：每个图事件转成一帧 SSE 推给前端。

    Tracer 必须显式 activate()。plan 节点里的 add_tokens 与 plan / verify / act
    的 trace.emit 都走 trace.current()，不激活就全部被静默丢弃——
    jsonl 里只剩 node_start / node_end，token 恒为 0。
    """
    g = build_graph(use_checkpoint=True)
    thread_id = sid or new_thread_id()
    cfg = {"configurable": {"thread_id": thread_id}}

    t = trace.Tracer(msg, model=MODEL, provider=PROVIDER, base_url=BASE_URL)
    t.activate()
    t0 = time.perf_counter()
    started: dict[str, float] = {}
    err: str | None = None

    try:
        yield _frame({"type": "open", "thread_id": thread_id})

        # 手动迭代而不用 for：Starlette 对同步生成器每次 next() 都在线程池里取
        # 一个新的 context，activate() 只做一次的话，第一个 yield 之后就失效了。
        # 每轮 next 之前重新 set，节点内部的 trace.current() 才能读到。
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
                yield _frame({"type": "error", "message": err})
                continue

            now = round((time.perf_counter() - t0) * 1000)
            if typ == "task":
                started[name] = time.perf_counter()
                yield _frame({"type": "node_start", "node": name,
                              "label": NODE_LABEL.get(name, name), "t": now})
                t.write("node_start", node=name)
            elif typ == "task_result":
                ms = round((time.perf_counter() - started.get(name, time.perf_counter())) * 1000)
                detail = _node_detail(name, pl.get("result"))
                yield _frame({"type": "node_end", "node": name,
                              "label": NODE_LABEL.get(name, name),
                              "ms": ms, "detail": detail, "t": now})
                t.write("node_end", node=name, ms=ms, detail=detail)
            # 节点内部的 plan / verify / act 事件不在这里补写——
            # 它们经由 trace.emit 落盘，靠上面每轮的 activate() 才能看到 tracer

        state = (g.get_state(cfg).values or {})
        done = {
            "type": "done",
            "final": state.get("final", ""),
            "final_text": state.get("final_text", ""),
            "blocked": state.get("blocked", []),
            "misquoted": state.get("misquoted", []),
            "cite": state.get("cite_stats", {}),
            "steps": state.get("steps", 0),
            "tools": state.get("tool_names", []),
            "evidence": _brief(state.get("evidence", []), state.get("steps", 0)),
            "trace_id": t.run_id,
            "thread_id": thread_id,
            "turn": state.get("turn", 1),
            "intent": state.get("intent", ""),
            "total_ms": round((time.perf_counter() - t0) * 1000),
        }
        yield _frame(done)
        t.write("result", steps=done["steps"], intent=done["intent"],
                blocked=done["blocked"], tools=done["tools"],
                final_head=done["final"][:300])
    except Exception as exc:  # 生成器里的异常必须变成一帧，否则前端只看到连接断
        err = f"{type(exc).__name__}: {exc}"
        t.write("stream_error", error=err)
        yield _frame({"type": "error", "message": err})
    finally:
        t.deactivate()
        try:
            t.close(error=err)
        except Exception:
            pass


@app.post("/api/diagnose/stream")
def diagnose_stream(req: DiagnoseIn):
    sid = _validate(req)
    return StreamingResponse(
        _sse(req.msg, sid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs")
def runs():
    items = []
    for p in trace.list_runs()[:30]:
        ev = trace.load(p)
        head = next((x for x in ev if x["event"] == "run_start"), {})
        end = next((x for x in ev if x["event"] == "run_end"), {})
        res = next((x for x in ev if x["event"] == "result"), {})
        items.append({
            "id": p.stem,
            "msg": head.get("user_msg", ""),
            "elapsed_ms": end.get("elapsed_ms", 0),
            "tokens": end.get("tokens_total", 0),
            # 流式接口不记 token（模型 usage 在节点内部，debug 事件不携带），
            # 前端要靠 note 区分"确实是 0"和"没记录"
            "note": head.get("note", ""),
            "steps": res.get("steps", 0),
            "blocked": res.get("blocked", []),
            "misquoted": res.get("misquoted", []),
        })
    return {"runs": items}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    # 原来是直接拼路径，只靠 .jsonl 后缀和 exists() 兜着；这里收紧成白名单
    if not _ID_RE.match(run_id):
        raise HTTPException(400, "run_id 不合法")
    p = trace.RUNS_DIR / f"{run_id}.jsonl"
    if not p.exists():
        raise HTTPException(404, "轨迹不存在")
    return {"events": trace.load(p)}
