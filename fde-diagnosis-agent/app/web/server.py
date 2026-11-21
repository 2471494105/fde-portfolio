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
from app.graph.build import build_graph, result_event, run
from app.graph.llm import BASE_URL, MODEL, PROVIDER
from app.graph.persistence import new_thread_id
from app.entry.labels import _brief, tool_cn
from app.entry.stream import iter_events

STATIC = Path(__file__).resolve().parent / "static"

# 只允许这两类标识符，顺便挡掉 ../../ 这类路径拼接
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

app = FastAPI(title="视觉检测设备故障诊断 Agent", version="1.0")


class DiagnoseIn(BaseModel):
    msg: str
    session_id: str | None = None


@app.get("/")
def index():
    # 加 no-cache：改了前端页面，浏览器拿旧缓存会让人以为改没生效（实测踩过）
    return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


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
    # 第三个出口，原来也手抄了一份，和 Web 流式那帧早就对不上：
    # 这里没有 stop_reason / check_rounds / device_ids / slots 也不全。
    # 公共字段统一从 result_event 取——final 出去时已经是序列化好的字符串，
    # 前端拿到的形态和以前一致。
    return {
        **result_event(state),
        "evidence": _brief(state.get("evidence", [])),
        "trace_id": state.get("trace_id", ""),
        "thread_id": state.get("thread_id", ""),
    }


def _frame(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False, default=str) + "\n\n"


def _sse(msg: str, sid: str | None):
    """SSE 编码层：帧由 app.entry.stream.iter_events 统一产出。

    抽层前（DOC-37 W1）这里既取图事件、又拼 SSE、还要为 Starlette 的线程池
    补 contextvar（每次 next 前重新 activate）；抽层后只剩 "data: {json}" 这一层编码。
    改造前抓过基线：22 帧（open + 9 组 node_start/node_end + done），改造后逐项对齐。
    """
    for frame in iter_events(msg, sid):
        yield _frame(frame)

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
    evs = trace.load(p)
    # 轨迹页是给人回看的：工具名补一份中文，原字段 name 一字不改（审计要对得上原始轨迹）
    for e in evs:
        if e.get("event") == "tool" and e.get("name"):
            e["name_cn"] = tool_cn(e["name"])
        elif e.get("event") == "plan" and e.get("calls"):
            e["calls_cn"] = [tool_cn(c) for c in e["calls"]]
    return {"events": evs}
