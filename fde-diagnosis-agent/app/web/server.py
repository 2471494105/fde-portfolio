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

STATIC = Path(__file__).resolve().parent / "static"

# 只允许这两类标识符，顺便挡掉 ../../ 这类路径拼接
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

app = FastAPI(title="视觉检测设备故障诊断 Agent", version="1.0")


class DiagnoseIn(BaseModel):
    msg: str
    session_id: str | None = None


def _as_obj(res):
    """把工具返回值归一化成对象，再往下算摘要。

    这里有两个坑叠在一起：
    \u2460 有些工具（如 query_device_profile）返回的是**已经序列化好的 JSON 字符串**，
       直接 json.dumps 会套第二层引号，前端 parse 出来是 str 而不是 dict；
    \u2461 evidence 里存的 result 不是原始对象，而是模型当时看到的那份带签名文本
       `[query_device_profile{device_id=CX-103}] {"device_id": ...}`。
       签名是给模型和审计看的，字段表只能从它后面的 JSON 算，所以先剥签名。
    两处不处理，界面上就只剩"原始 JSON 片段"——2026-11-20 现场反馈的就是这个现象。
    """
    if not isinstance(res, str):
        return res
    s = res.strip()
    m = re.match(r"^\[.+?\]\s*([\{\[].*)$", s, re.S)
    if m:
        s = m.group(1).strip()
    else:
        # 签名里带方括号参数时上面的懒惰匹配会失手，退一步按 "] {" 切
        i = s.rfind("] ")
        if i > 0 and s[i + 2:].lstrip()[:1] in "{[":
            s = s[i + 2:].strip()
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


# ============ 人话翻译层 ============
#
# 面向现场的展示转换都在这里做，不散到工具实现里：
#   工具返回的字段名（device_id / group_by / false_positive_rate）是给模型和校验层看的，
#   现场工程师看到 "device_id=CX-103" 等于没看到。摘要和字段表必须拿**完整对象**来算，
#   前端只有 json.dumps(...)[:600] 的残片，parse 必然失败（见 _summarize 的注释）。
#
# 注意：本文件不在 app/meta.py 的 BEHAVIOR_FILES 里，改它不动 code_hash：
# 它是展示层，不参与模型行为，也不参与评测判定。

INTENT_CN = {
    "diagnose": "诊断故障",
    "lookup": "查设备档案",
    "knowledge": "查手册知识",
    "action": "执行动作（建单 / 派工）",
    "refuse": "拒答（超出授权或范围）",
    "clarify": "追问补充信息",
}


def intent_cn(v) -> str:
    """意图枚举转中文。认不出的原样返回——宁可露出英文，也不要编一个中文意思。"""
    return INTENT_CN.get((v or "").strip(), "")


# 工具名也要说人话：query_device_profile 对现场是天书。
# 认不出的原样返回，审计时仍能对上原始工具名（前端 title 里也保留原名）。
TOOL_CN = {
    "query_device_profile": "查设备档案",
    "query_defect_log": "查误判记录",
    "query_calibration": "查标定记录",
    "query_light_usage": "查光源使用",
    "search_manual": "查手册",
    "check_doc_conflict": "查文档冲突",
    "query_work_order": "查工单",
    "create_work_order": "建工单",
}


def tool_cn(name) -> str:
    return TOOL_CN.get((name or "").strip(), name or "")


FIELD_CN = {
    "device_id": "设备编号", "state": "查询状态", "line": "产线", "station": "工位",
    "camera_model": "相机型号", "lens_model": "镜头", "light_model": "光源型号",
    "install_date": "安装日期", "maintenance_cycle_days": "保养周期", "log_count": "误判记录总数",
    "latest_log_at": "最近一次误判",
    "time_from": "统计起点", "time_to": "统计终点", "group_by": "分组维度",
    "total": "误判总数", "false_positive_count": "误判条数", "false_positive_rate": "误判率",
    "groups": "分组明细", "samples": "抽样明细", "key": "分组", "count": "条数",
    "false_positive": "其中误判", "rate": "误判率",
    "ts": "时间", "defect_type": "缺陷类型", "confidence": "置信度", "batch": "批次",
    "records": "标定记录", "last_calibration_at": "最近标定", "days_since_last": "距上次标定",
    "drift_trend": "漂移趋势", "drift_px": "漂移(px)", "drift_mm": "漂移(mm)",
    "result": "结果", "operator": "操作人",
    "cumulative_hours": "累计点亮", "rated_life_hours": "额定寿命", "over_life_hours": "已超寿命",
    "remaining_hours": "剩余寿命", "brightness_set": "当前亮度设定", "last_replaced_at": "上次更换",
    "brightness_history": "亮度调整历史", "value": "亮度值", "degradation_estimate": "衰减判断",
    "scope": "查询范围", "orders": "工单", "order_id": "工单号", "root_cause": "根因",
    "root_cause_code": "根因归类", "severity": "严重度", "status": "状态",
    "assignee_group": "派给", "created_at": "建单时间", "actions": "处置动作",
    "evidence_refs": "依据引用", "note": "说明",
    "query": "检索词", "chunks": "命中片段", "text": "正文", "source_doc": "来源文档",
    "doc_type": "文档类型", "section": "章节", "score": "相似度",
    "param_name": "参数名", "has_conflict": "是否存在冲突", "value_count": "取值种类",
    "conflicts": "冲突明细", "source": "来源", "likely_authoritative": "可能权威的依据",
    "normalized_from": "参数名归一化自", "limit": "条数上限", "top_k": "取前几条",
    "error": "错误",
}

# 同名不同义的字段：count 在分组表里是"条数"，在工单查询里是"工单数"
FIELD_OVERRIDE = {
    "query_work_order": {"count": "工单数"},
    # 113 是区间内记录总数，27 才是其中被判误判的——两个都叫"误判"会看不明白
    "query_defect_log": {"total": "记录总数", "false_positive_count": "其中误判"},
    # 冲突明细里的 value 是"这份文档给的取值"，不是亮度值
    "check_doc_conflict": {"value": "取值"},
}

# 每条工具的主列表：不按"哪个最长"选，否则 defect_log 的 5 条抽样会盖掉 3 条分组
PREFERRED_LIST = {
    "query_device_profile": None,
    "query_defect_log": "groups",
    "query_calibration": "records",
    "query_light_usage": "brightness_history",
    "search_manual": "chunks",
    "check_doc_conflict": "conflicts",
    "query_work_order": "orders",
    "create_work_order": None,
}

# 这几句是给现场的操作指引，截断就把结论截没了（"不要自行挑选"就在后半句）
FULL_TEXT_KEYS = {"note", "degradation_estimate", "error", "recommendation"}

# 列表行的字段优先级：先放人最关心的，凑满四项
ROW_PRIORITY = ("key", "order_id", "source", "station", "ts", "count",
                "false_positive", "rate", "value", "drift_px", "status", "severity")

VALUE_CN = {
    "station": "按工位", "day": "按天", "batch": "按批次", "defect_type": "按缺陷类型",
    "found": "存在", "not_found": "未找到", "pass": "通过", "fail": "未通过",
    "prose": "手册正文", "ok": "成功", "empty": "无结果", "tool_error": "工具报错",
    "high": "高", "medium": "中", "low": "低",
}

UNITS = {
    "maintenance_cycle_days": " 天", "days_since_last": " 天",
    "cumulative_hours": " 小时", "rated_life_hours": " 小时",
    "over_life_hours": " 小时", "remaining_hours": " 小时",
}

_RE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}) 00:00:00$")


def _label(tool: str, key: str) -> str:
    return (FIELD_OVERRIDE.get(tool) or {}).get(key) or FIELD_CN.get(key) or key


def _val(key: str, v) -> str:
    """把值写成现场能读的样子：比率转百分比、小时/天带单位、整点日期去掉 00:00:00。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        if key.endswith("rate") and 0 <= float(v) <= 1:
            return f"{float(v) * 100:.1f}%"
        if key == "score":
            return f"{float(v):.3f}"
        if key in UNITS:
            return f"{int(v)}{UNITS[key]}"
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, str):
        m = _RE_DATE.match(v)
        if m:
            return m.group(1)
        if v in VALUE_CN:
            return VALUE_CN[v]
        # group_by 常是逗号拼接的多维度：station,defect_type,batch
        # 整体查表查不到，就逐项翻再拼回去，别把三个英文枚举甩给现场
        if "," in v:
            parts = [VALUE_CN.get(x.strip(), x.strip()) for x in v.split(",")]
            if any(p in VALUE_CN.values() for p in parts):
                return "、".join(parts)
        if key in FULL_TEXT_KEYS:
            return v
        return v[:70] + "…" if len(v) > 70 else v
    return str(v)


def _row_text(tool: str, row) -> str:
    """列表里的一条压成一行：按优先级挑字段，跳过长正文，最多四对。"""
    if not isinstance(row, dict):
        return _val("", row)
    keys = []
    for k in ROW_PRIORITY:
        if k in row and not isinstance(row[k], (list, tuple, dict)):
            keys.append(k)
    for k in row:
        if k in keys or k in ("text", "by_param"):
            continue
        if isinstance(row[k], (list, tuple, dict)):
            continue
        keys.append(k)
    parts = [f"{_label(tool, k)} {_val(k, row[k])}" for k in keys[:4]]
    return " · ".join(parts)


def _view(tool: str, res) -> dict:
    """一条工具返回的中文视图：字段表 + 主要列表的少量样例。

    只挑标量字段，列表单独成块——把 200 条记录铺到页面上没有意义，
    现场要的是"有几个、长什么样"。
    """
    res = _as_obj(res)
    if isinstance(res, str):
        return {"fields": [], "list": None, "note": res[:80]}
    if isinstance(res, list):
        return {"fields": [], "list": {"label": "记录", "count": len(res),
                                        "items": [_row_text(tool, r) for r in res[:3]]}}
    if not isinstance(res, dict):
        return {"fields": [], "list": None, "note": str(res)[:80]}

    fields = []
    lists = []
    for k, v in res.items():
        if isinstance(v, (list, tuple)):
            lists.append((k, v))
        elif isinstance(v, dict):
            continue
        elif len(fields) < 12:
            fields.append({"label": _label(tool, k), "value": _val(k, v)})

    out = {"fields": fields, "list": None}
    if lists:
        pref = PREFERRED_LIST.get(tool)
        picked = next((kv for kv in lists if kv[0] == pref), None) if pref else None
        if picked is None:
            picked = max(lists, key=lambda kv: len(kv[1]))
        k, v = picked
        out["list"] = {"label": _label(tool, k), "count": len(v),
                       "items": [_row_text(tool, r) for r in v[:3]]}
    return out


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
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        out.append({
            "tool": e["tool"],
            "tool_cn": tool_cn(e["tool"]),
            "args": e["args"],
            # 参数也翻译：group_by=station 这种，现场看不出是"按工位分组"
            "args_view": [{"label": _label(e["tool"], k), "value": _val(k, v)}
                          for k, v in args.items()],
            "ok": ok,
            "error": (res.get("error") if not ok else None),
            "summary": _summarize(res),
            # 中文字段表：拿完整对象算，前端只负责排版（head 是截断串，parse 靠不住）
            "view": _view(e["tool"], res),
            "head": json.dumps(res, ensure_ascii=False, default=str)[:600],
            "current": id(e) in cur_ids,
        })
    return out


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


def _frame(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False, default=str) + "\n\n"


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
        "meta_intent": base.get("intent"),
        "type": "done",
        **base,
        # 意图的中文写法单独给一栏，不覆盖原值：审计仍然要看到 intent 枚举本身
        "intent_cn": intent_cn(base.get("intent")),
        "evidence": _brief(state.get("evidence", []), state.get("steps", 0)),
        "trace_id": trace_id,
        "thread_id": thread_id,
        "total_ms": elapsed_ms,
    }


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
        # 字段定义在 _done_event：公共部分从 result_event 派生，不再手抄。
        # 顺带补上 stop_reason / check_rounds——前端以前拿不到这两个，
        # 分不清"结论完整"和"结论是被预算掐断的半成品"。
        done = _done_event(state, trace_id=t.run_id, thread_id=thread_id,
                           elapsed_ms=round((time.perf_counter() - t0) * 1000))
        yield _frame(done)
        # 与 run() 共用 result_event：Web 跑的轨迹也必须能离线复算。
        # 之前这里只写 final_head，同一次运行走 Web 就没有结论全文，
        # 事后想复算「这条为什么被拦」只能重跑——而重跑的结果不一定一样。
        t.write("result", thread_id=thread_id, **result_event(state))
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
    evs = trace.load(p)
    # 轨迹页是给人回看的：工具名补一份中文，原字段 name 一字不改（审计要对得上原始轨迹）
    for e in evs:
        if e.get("event") == "tool" and e.get("name"):
            e["name_cn"] = tool_cn(e["name"])
        elif e.get("event") == "plan" and e.get("calls"):
            e["calls_cn"] = [tool_cn(c) for c in e["calls"]]
    return {"events": evs}
