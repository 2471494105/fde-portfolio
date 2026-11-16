"""图节点。

设计原则：L2 方向判断完全交给模型——代码里不出现"如果某工位误判率高就查光源"这类分支。
程序只做三件模型做不好的事：L1 信息补全、工具执行、结论校验。
"""

import datetime
import json
import re
import time

from openai import OpenAI

from app import trace
from app.graph.llm import CALL_TIMEOUT, MODEL, get_client
from app.tools import TOOL_IMPLS, TOOL_SCHEMAS
from app.tools.diagnosis import query_defect_log, query_device_profile
from app.tools.manual import check_doc_conflict, search_manual
from app.tools.workorder import query_work_order

MAX_STEPS = 12

DEVICE_RE = re.compile(r"CX-\d{3}", re.IGNORECASE)
SYSTEM_PROMPT = """你是产线上的视觉检测设备诊断助手（FDE）。

你的工作：
1. 确认设备是否存在（query_device_profile）。查不到就不要假设它存在。
2. 判断故障方向。核心方法是看误判是否集中：
   - 集中在一个工位 → 该工位的镜头/光源/标定是独立部件，偏硬件
   - 集中在某个物料批次或某个缺陷类型 → 模型和阈值是所有工位共用的，偏算法
   - 误判率在某个时间点突然跳变 → 查该时点前后的标定记录
   - 缓慢上升、标定正常、无告警 → 考虑镜头污染或模型漂移
3. 收集对应证据：光源问题查 query_light_usage，标定问题查 query_calibration，
   参数标准查 search_manual。
4. 结论必须建立在工具返回的数字上，不要凭经验补充数字。
5. 引用参数取值时，如果不同文档说法不一致，用 check_doc_conflict 确认，
   有冲突就如实上报，不要自己挑一个继续用。
6. 根因明确、处置动作明确时才调 create_work_order。
   用户只说"查一下""看看怎么回事""有点问题"时不要建单——那是诊断请求，不是派工请求。
   实测反例：用户说"CX-101 有点问题，你查一下"，模型自己建了单，被判越界。
7. 用户陈述的事实（工位数量、设备型号、批次号、阈值、周期）如果与工具返回不一致，
   必须在回答里明确指出"你说的是 A，工具显示是 B"，不要沿用用户的说法往下推理。
   用户给的前提可能是错的，顺着错前提得出的结论比说"不知道"更糟。
8. 但"指出不一致"不等于"拒绝执行"。用户明确要求某个动作（建单、派工）且根因已经明确时，
   就执行；有疑点的参数在结论里标出来让用户定，不要替用户决定"这事我不做"。
   你是助手，不是审批人。
9. 不要在正文里写行动计划。要调用工具就直接发起 tool_calls；不打算再查了就写结论。
   "接下来我调用 query_calibration"这种句子不会被执行，只会让这一轮白白空转。
10. 结论必须输出成 JSON，而且只输出这一个 JSON，不要包 ```json``` 代码块。
   数据结构：
{
  "conclusion": "一句话根因，不要在这里写数字",
  "claims": [
    {"text": "含数字的原话", "value": 90, "unit": "天", "ref": "1#maintenance_cycle_days"},
    {"text": "误判率 58.82%", "value": 58.82, "unit": "%", "ref": "3#groups[0].rate"},
    {"text": "复位后预计降到 6%~11%", "value": 6, "unit": "%", "ref": null}
  ],
  "advice": ["先重新标定再复测", "把 11-07 前后的标定记录调出来"]
}
   规则：
   - 结论里每出现一个数字，就单独占一条 claim。三个数字要写三条，
     不许挤在一句话里共用一个 ref——这是最关键的一条。
   - value 只写数字本身：58.82% 写成 58.82，unit 写 "%"；90 天写成 90，unit 写 "天"。
   - ref 写这个数的出处：第 k 条工具返回的字段路径，k 按你实际看到的顺序从 1 数。
     字段路径照工具返回的 JSON 原样写，顶层 "2#maintenance_cycle_days"、
     数组下标 "3#groups[0].count"、嵌套 "6#records[0].drift_px"。
   - 自己算出来的值（比值 / 差值 / 加总）ref 写 null，并在 text 里写算式：
     {"text": "74%（=20/27）", "value": 74, "unit": "%", "ref": null}
   - 自己定的验收目标、假设值，ref 也写 null。
   - 没有数字的论述写进 conclusion 或 advice，不要塞进 claims。
   - ref 写错比写 null 更糟：拿不准就不写字段路径，写 null。

回答用中文。conclusion 说根因，claims 放全部带数字的证据，advice 说处置。
"""


def intake(state):
    msg = state.get("user_msg", "")
    m = DEVICE_RE.search(msg)
    device_id = m.group(0).upper() if m else ""
    turn = int(state.get("turn") or 0) + 1
    trace.emit("intake", device_id=device_id, matched=bool(m), msg_len=len(msg), turn=turn)
    # 这四个每轮重写：不清的话上一轮的结论/告警/步数会污染本轮判断。
    # messages 和 evidence 相反，靠 reducer 累积——多轮必须保留。
    return {
        "device_id": device_id,
        "messages": [{"role": "user", "content": msg}],
        "steps": 0,
        "final": "",
        "blocked": [],
        "tool_names": [],
        "turn": turn,
        "plan_retry": 0,
    }


UNDERSTAND_PROMPT = """你在给一台视觉检测设备诊断系统做第一层意图判断。只做分类，不做诊断，不要调用任何工具。

判断用户这句话属于哪一类：

- diagnose：用户描述了异常现象，想找原因。
  例：误判率高、良率掉了、夜班更差、从某天开始变多、是不是光源导致的
- lookup：用户想看数据、配置、参数、记录本身，没有要找原因的意思。
  例：给我看下数据、参数是多少、配置是什么、最近记录
- knowledge：用户在问规则、标准、含义、操作步骤。这类问题不依赖具体设备也能回答。
  例：E-101 是什么意思、标定周期多久、亮度标准值是多少、怎么清洁镜头
- action：用户要求执行一个动作（建单、派工、登记、通知），不是在问原因。
  例：帮我建个工单、派给设备科、登记一下、再建一次刚才那个单
- clarify：信息不足以判断以上任何一类

三条硬规则：
1. 句子里出现设备号不代表就是 diagnose，要看用户有没有描述异常、有没有问原因。
2. 用户带着猜测来问（例如「是不是阈值太严」）仍然属于 diagnose，不要因为他在猜就判成 clarify。
3. phenomenon 只记录用户实际说出口的现象或猜测。用户没说就留空字符串，禁止替用户补充。
   但用户明确要求查看、排查、诊断时（例如「你查一下」「帮我看看」），即使没描述现象，
   也应判为 diagnose——可以先去取数据再判断。
4. 如果用户问的是某个参数该取什么值（亮度、周期、阈值、速度、曝光），把参数名写进 param_names。

只输出 JSON，不要任何解释文字：
{
  "intent": "diagnose|lookup|knowledge|action|clarify",
  "device_ids": ["CX-104"],
  "alarm_codes": ["E-101"],
  "param_names": ["光源亮度设定"],
  "phenomenon": "用户描述的异常现象（概括原话），没有就填空字符串",
  "missing": ["还缺什么信息才能继续"],
  "reason": "一句话说明凭什么这么判"
}"""

# 模型判为诊断/查询但输出字段不自洽时的降级说明
_INTENT_ENUM = {"diagnose", "lookup", "knowledge", "action", "clarify"}


def _parse_json_loose(raw):
    """从模型输出里尽量抠出 JSON 对象，抠不出来返回 None。

    为什么需要这一步：中转站偶发会在 JSON 外面裹 ```json 代码块，或前后带说明文字，
    json.loads 直接失败。而原来的写法把"调用"和"解析"放在同一个 try 里，
    一次格式抖动就等于整层意图判断丢掉，只能退回按正则设备号兜底——
    但这个项目最强的指标恰恰是意图判对率，不该被一次格式抖动带走。
    """
    if not raw:
        return None
    s = raw.strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(s[i:j + 1])
        except json.JSONDecodeError:
            pass

    return None


def understand(state):
    """第一层意图理解：让模型自己判断用户在问什么。

    程序在这里不做语义判断，只做两件模型做不好的事：
    1. 校验模型输出是否自洽（判了 diagnose 却没识别出用户说的现象，说明这个 diagnose 是猜的）
    2. 调用失败时退回旧逻辑，不让新增的这一层把原本可用的路径搞挂
    """
    msg = state.get("user_msg", "")
    hint = state.get("device_id", "")
    prev_q = (state.get("last_question") or "").strip()
    slots = state.get("slots") or {}

    hint_line = ""
    if hint:
        hint_line += f"\n\n[系统预抽] 正则从原话里抽到候选设备号：{hint}（仅供参考，可能不准）"
    if prev_q:
        hint_line += (
            f"\n\n[上一轮你问过用户] {prev_q}\n"
            "用户这句话大概率是在回答它：先判断它在补哪个槽位，补上就继续往下走，不要重问一遍。"
        )
    if slots.get("device_id") or slots.get("phenomenon"):
        hint_line += (
            f"\n[已确认] 设备={slots.get('device_id') or '未定'}；"
            f"现象={slots.get('phenomenon') or '未定'}。已确认的不要让用户再说一次。"
        )

    fallback_intent = "diagnose" if hint else "clarify"
    fallback_reason = ""
    data = None
    # 只对"解析失败"重试一次；网络/超时失败不重试，免得把延迟叠成两倍。
    for attempt in (1, 2):
        try:
            resp = get_client().chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": UNDERSTAND_PROMPT},
                    {"role": "user", "content": msg + hint_line},
                ],
                temperature=0,
                timeout=CALL_TIMEOUT,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            fallback_reason = f"意图判断调用失败（{type(exc).__name__}），退回按设备号判断"
            break

        usage = getattr(resp, "usage", None)
        if trace.current() and usage:
            trace.current().add_tokens(
                (getattr(usage, "prompt_tokens", 0) or 0)
                + (getattr(usage, "completion_tokens", 0) or 0)
            )

        raw = resp.choices[0].message.content or ""
        data = _parse_json_loose(raw)
        if data is not None:
            break

        # 解析不出来：留证，便于以后判断是中转站裹了壳、还是模型输出跑偏
        trace.emit("intent_parse_retry", attempt=attempt, head=raw[:80])

    if data is None:
        data = {
            "intent": fallback_intent,
            "device_ids": [hint] if hint else [],
            "phenomenon": "",
            "missing": [],
            "reason": "",
        }
        if not fallback_reason:
            fallback_reason = "意图判断输出不是合法 JSON（已重试一次），退回按设备号判断"

    # ---- 程序级一致性校验：不替模型判断语义，只查它自不自洽 ----
    intent = str(data.get("intent") or "").strip().lower()
    if intent not in _INTENT_ENUM:
        intent = "clarify"

    ids = []
    for x in (data.get("device_ids") or []):
        s = str(x).strip().upper()
        if DEVICE_RE.fullmatch(s):
            ids.append(s)
    prev_dev = (slots.get("device_id") or "").strip().upper()
    if not ids and hint:
        ids = [hint]
    # 槽位继承：本轮没提设备、但上一轮确认过 → 沿用，只对需要设备的意图生效。
    # clarify 不继承：用户什么都没说清时，别拿上一轮的设备开始跑诊断。
    if not ids and prev_dev and intent in ("diagnose", "lookup", "action"):
        ids = [prev_dev]

    phen = str(data.get("phenomenon") or "").strip()
    switched = bool(ids and prev_dev and ids[0] != prev_dev)
    # 换了设备又没说新现象 → 不要把 A 的现象套到 B 上
    if not phen and not switched:
        phen = str(slots.get("phenomenon") or "").strip()
    reason = fallback_reason or str(data.get("reason") or "").strip()

    # 只校验"有没有诊断对象"这一条硬矛盾。
    # "用户没说清现象"不算矛盾——可以先取数据再判断，所以不在这里降级。
    if intent == "diagnose" and not ids:
        intent = "clarify"
        reason = "判为诊断但没有设备号，无法确定诊断对象，降级为追问"
    elif intent == "lookup" and not ids:
        intent = "clarify"
        reason = "判为查询数据但没有指定设备，降级为追问"
    elif intent == "action" and not ids:
        intent = "clarify"
        reason = "判为动作请求但没有设备号，降级为追问"

    trace.emit(
        "understand",
        intent=intent,
        device_ids=ids,
        phenomenon=phen[:120],
        missing=list(data.get("missing") or []),
        reason=reason[:160],
    )

    return {
        "intent": intent,
        "device_id": ids[0] if ids else "",
        "device_ids": ids,
        "phenomenon": phen,
        "missing": list(data.get("missing") or []),
        "param_names": [str(p).strip() for p in (data.get("param_names") or []) if str(p).strip()],
        "intent_reason": reason,
        # slots 是合并写：本轮确认的覆盖上去，没确认的保留旧值
        "slots": {
            "device_id": ids[0] if ids else prev_dev,
            "phenomenon": phen,
        },
    }


def route_after_understand(state):
    intent = state.get("intent", "clarify")
    if intent in ("diagnose", "action"):
        return "plan"
    if intent == "lookup":
        return "lookup"
    if intent == "knowledge":
        return "knowledge"
    return "ask_user"


def ask_user(state):
    """追问。文案按「缺什么」生成，避免模型面对空信息又自己编一个假设。"""
    ids = state.get("device_ids") or []
    phen = state.get("phenomenon", "")
    missing = [m for m in (state.get("missing") or []) if m]

    if ids and not phen:
        final = (
            f"{ids[0]} 你想先看哪一方面？\n\n"
            "- 想排查异常：说一句现象，比如哪类缺陷误判多、从什么时候开始、哪个班次更重\n"
            "- 只想看数据：回一句「看数据」，我直接把最近的档案和误判统计给你"
        )
    elif ids:
        final = "请再具体一点：你想让我查什么、看什么？"
    else:
        final = (
            "需要两个信息才能开始：\n\n"
            "1. 设备编号（当前库里有 CX-101 ~ CX-105）\n"
            "2. 你想做什么——排查异常、看数据，还是查手册里的规则条款"
        )

    trace.emit("ask_user", intent=state.get("intent", ""), device_ids=ids,
               phenomenon=phen[:80], missing=missing,
               reason=state.get("intent_reason", ""))
    # 记下追问原话：下一轮的 understand 要靠它判断"用户这句在回答什么"
    return {"final": final, "last_question": final}


# 模型本轮能看到几轮。这一个数同时决定两件必须严格相等的事：
#   1. _context_messages 送几轮 messages   —— 模型据此数它看到的第 k 条工具返回
#   2. verify 允许哪几轮的 evidence 参与溯源 —— 校验层据此取 ev[k-1]
# 两处一旦不一致，k 就整体错位：模型数的第 3 条，校验层按第 5 条查。
# 表现是「引用率突然归零」或「大面积 cite_oob」，看不出是配置问题，
# 第一反应会去怀疑模型——所以只能有一个常量，两处都从 _ctx_window 派生。
CTX_VISIBLE_TURNS = 2
MAX_CTX_TURNS = CTX_VISIBLE_TURNS      # 兼容旧名：scripts/check_ctx.py 和文档在用


def _ctx_window(cur_turn):
    """本轮可见的轮号闭区间 (最早, 最晚)。上下文裁剪与证据过滤的唯一真相源。"""
    lo = max(1, int(cur_turn) - CTX_VISIBLE_TURNS + 1)
    return lo, int(cur_turn)


def _context_messages(state):
    """送给模型的上下文窗口。

    为什么要裁：每轮 messages 会追加 user/assistant/tool 若干条，单条 tool 结果最多 4000 字。
    不裁的话 10 轮之后一次 plan 的 prompt 能到几万 token，成本和延迟都不可接受。

    裁剪单位是「轮」不是「条」：从 user 消息处切，保证 assistant.tool_calls 和紧随其后的
    tool 消息不被切散——切散了 OpenAI 直接报错（tool 消息找不到对应的 tool_call_id）。

    保留轮数必须等于 CTX_VISIBLE_TURNS，和 verify 的证据过滤是同一个数。
    """
    msgs = list(state.get("messages") or [])
    starts = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    if len(starts) <= CTX_VISIBLE_TURNS:
        return msgs
    cut = starts[-CTX_VISIBLE_TURNS]
    head = msgs[:1] if cut > 0 else []      # 第一句原始诉求留着，别丢掉上下文起点
    return head + msgs[cut:]


_PENDING_WORDS = ("调用", "查询", "查一下", "看一下", "确认", "检查", "核对", "再查", "还需要")
_TOOL_NAME_PAT = re.compile("|".join(re.escape(n) for n in TOOL_IMPLS))


def _last_assistant_content(state):
    for m in reversed(state.get("messages") or []):
        if m.get("role") == "assistant":
            return m.get("content") or ""
    return ""


def _pending_call(content):
    """模型把"要调工具"写进正文，却没真发起调用。

    qwen2.5:7b 上实测到两种写法：
      "接下来，我们调用 query_calibration 检查…"
      "调用 query_calibration 工具查询1号工位的标定记录。"
    tool_calls 都是空的，图当成已收尾，证据链断在半路。

    判据：只看结尾 160 字，必须同时出现真实工具名和动作意向词。
    代价是正常结论末尾写"建议调用 query_calibration 复核"也会被判一次 pending，
    多花一次调用——这个误判成本比"漏掉未完成的取证"低得多。
    """
    if not content:
        return False
    tail = content[-160:]
    if not _TOOL_NAME_PAT.search(tail):
        return False
    return any(w in tail for w in _PENDING_WORDS)


def plan(state):
    used = state.get("steps", 0)
    # system prompt 必须逐字恒定，才能命中前缀缓存。
    # 会变的内容（工具预算、跨轮摘要、重试提醒）一律追加到 messages 末尾——
    # 之前把预算拼在 system 开头，步数一涨第一句就变，后面 2000+ token 全部按未命中计费，
    # 实测一条 8 步的用例 5 次 plan 缓存命中率是 0/2265、0/3203、0/4591、0/5265、0/5513。
    sys_prompt = SYSTEM_PROMPT
    notes = [
        f"[工具预算] 已用 {used}/{MAX_STEPS} 次。剩余 3 次及以下时请停止取证、直接给结论；"
        "如果用户本轮已明确要求某个动作（例如建单），在根因已明确时优先完成该动作。"
    ]
    summary = (state.get("session_summary") or "").strip()
    if summary:
        # 裁掉的历史靠这段一句话摘要保留，不然模型不知道前面查过什么
        notes.append("[前几轮发生了什么]\n" + summary)
    # 上一次只写了"接下来调用 X"却没真调用 → 这一轮追加提醒，并要求它直接发 tool_calls
    retry = int(state.get("plan_retry") or 0)
    if retry == 0 and _pending_call(_last_assistant_content(state)):
        notes.append(
            "[系统提醒] 你上一次回复里写了要调用工具，但没有真正发起调用。"
            "工具只能通过 tool_calls 发起，写在正文里的调用不会被执行。"
            "这一次要么直接发起 tool_calls，要么给出最终结论。"
        )
        retry = 1

    msgs = [{"role": "system", "content": sys_prompt}] + _context_messages(state)
    msgs.append({"role": "user", "content": "\n\n".join(notes)})
    client: OpenAI = get_client()
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=msgs,
        tools=TOOL_SCHEMAS,
        temperature=0,
    )
    ms = round((time.perf_counter() - t0) * 1000)
    m = resp.choices[0].message

    usage = getattr(resp, "usage", None)
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    # DeepSeek 的上下文缓存：system prompt 前缀固定，命中后按约 1/10 计价。
    # 这两个字段只对 API 有效，本地恒为 0——记下来才能证明省钱省在哪
    cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    if trace.current():
        trace.current().add_tokens(pt + ct)

    out = {"role": "assistant", "content": m.content or ""}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in m.tool_calls
        ]

    trace.emit(
        "plan",
        ms=ms,
        prompt_tokens=pt,
        completion_tokens=ct,
        cache_hit=cache_hit,
        cache_miss=cache_miss,
        calls=[tc.function.name for tc in (m.tool_calls or [])],
        content_head=(m.content or "")[:300],
        steps_before=used,
    )
    return {"messages": [out], "plan_retry": retry}


def route_after_plan(state):
    last = state["messages"][-1]
    if last.get("tool_calls"):
        if state.get("steps", 0) >= MAX_STEPS:
            return "finalize"
        return "act"
    # 正文写了"接下来调用 X"但 tool_calls 为空：不认它是结论，退回 plan 一次。
    # plan_retry 已经在 plan 里置位，所以最多退一次，不会死循环。
    if int(state.get("plan_retry") or 0) == 0 and _pending_call(last.get("content")):
        return "plan"
    return "verify"


# 压缩档位：(数组保留项数, 字符串截断长度)。由宽到紧依次尝试。
# 为什么是"多档"而不是一个数：工具返回的体积随业务数据涨，写死一个上限
# 迟早会被撞破。撞破时正确做法是丢内容，而不是切字符——切字符会让 JSON
# 不再是 JSON，下游 _load_obj 直接解析失败。
_SLIM_LEVELS = ((8, 300), (4, 150), (2, 80))


def _slim_result(obj, depth=0, arr=8, strlen=300):
    """压缩送进模型的工具返回。

    为什么不直接 [:4000] 硬截：json 被截断会切掉括号，模型读到的是一段坏 JSON，
    比读少一点更糟；而且 defect_log 的 groups 数组里，前面几条往往是正常工位，
    关键异常在后面，按位置截会正好把要的证据扔掉。

    这里按结构压：数组留前 arr 项并注明总条数，长字符串截 strlen，标量原样保留。
    参数化是为了让 _payload 在超限时逐档收紧，始终保持结构合法。
    """
    if depth > 3:
        return str(obj)[:strlen]
    if isinstance(obj, dict):
        return {k: _slim_result(v, depth + 1, arr, strlen) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_slim_result(v, depth + 1, arr, strlen) for v in obj[:arr]]
        if len(obj) > arr:
            items.append(f"...共 {len(obj)} 项，仅送前 {arr} 项")
        return items
    if isinstance(obj, str):
        return obj[:strlen]
    return obj


def _payload(res, limit=1600):
    """统一出口：模型看到的文本 与 evidence 存的，必须是同一份。

    返回 (text, truncated)。text 永远是**合法 JSON**——这是硬约束，因为
    verify 要用 _load_obj 解析它取字段；一旦不是 JSON，所有引用都会判
    ref_bad，报出来是"模型引用写错"，实际是证据坏了，归因完全反了
    （实测：一条 1606 字的 defect_log 返回就能触发，正确引用全被判失败）。

    逐档收紧结构；三档都压不住时不再切字符串，改为降级成一个仍可解析的
    包装对象，把能留的内容放 preview 里。模型看到 preview 仍可引用其中的
    数字（_value_match 支持字符串内抽数），校验层也能据此判是"证据截断"
    而不是"模型写错"。
    """
    for arr, strlen in _SLIM_LEVELS:
        text = json.dumps(_slim_result(res, 0, arr, strlen),
                          ensure_ascii=False, default=str)
        if len(text) <= limit:
            return text, False

    # 结构已压到最小仍超限：降级为合法 JSON，明细进 preview
    full = json.dumps(res, ensure_ascii=False, default=str)
    out = {
        "_payload_overflow": True,
        "note": "返回体过大，已省略结构化明细；preview 是原始内容前段，可从中读数",
        "top_keys": list(res.keys())[:12] if isinstance(res, dict) else [],
        "preview": "",
    }
    room = limit - len(json.dumps(out, ensure_ascii=False)) - 8   # 留转义余量
    n = max(0, room)
    out["preview"] = full[:n]
    text = json.dumps(out, ensure_ascii=False)
    while n > 0 and len(text) > limit:      # 转义会撑长，逐步收窄直到不超
        n = int(n * 0.8)
        out["preview"] = full[:n]
        text = json.dumps(out, ensure_ascii=False)
    return text, True


def _owned_device_ids(state):
    """本轮允许工具访问的设备号集合。

    来源只有 understand 的产出：它已经把"用户原话里抽到的设备"和"上一轮确认的设备"
    合并进 device_ids（槽位继承只在 diagnose / lookup / action 时生效）。
    所以这里不再单独读 slots——读 slots 会把"用户已经切换对象"的旧设备重新放进来，
    那正是归属校验要挡住的情况。
    """
    ids = {str(x).strip().upper() for x in (state.get("device_ids") or []) if str(x).strip()}
    d = str(state.get("device_id") or "").strip().upper()
    if d:
        ids.add(d)
    return ids


def act(state):
    last = state["messages"][-1]
    out_msgs, evidence, names = [], [], []

    for tc in last["tool_calls"]:
        name = tc["function"]["name"]
        args = {}
        t0 = time.perf_counter()
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
            res = {"error": "参数 JSON 解析失败", "raw": tc["function"]["arguments"]}
        else:
            impl = TOOL_IMPLS.get(name)
            if not impl:
                res = {"error": f"未知工具 {name}"}
            else:
                owned = _owned_device_ids(state)
                want = str(args.get("device_id") or "").strip().upper()
                # 归属校验：设备号必须是用户在本轮或上一轮真正提过的。
                # 只做存在性校验不够——库里每个号都"存在"，模型可以拿任意一个合规的号去查别的设备，
                # 工具会如实返回那台设备的数据，界面上看不出任何异常。W0 写了这条，正式图里漏了。
                if want and want not in owned:
                    res = {
                        "error": f"设备 {want} 不是本次对话涉及的设备",
                        "owned_devices": sorted(owned),
                        "hint": (
                            "只能查询用户提到的设备。若只能靠别的设备回答，先向用户确认编号，"
                            "不要自行更换对象。"
                        ),
                    }
                else:
                    try:
                        res = impl(**args)
                    except TypeError as exc:
                        res = {"error": f"参数不匹配：{exc}"}
                    except Exception as exc:
                        res = {"error": f"{type(exc).__name__}: {exc}"}
        ms = round((time.perf_counter() - t0) * 1000)

        ok = not (isinstance(res, dict) and "error" in res)
        res_json = json.dumps(res, ensure_ascii=False, default=str)
        trace.emit(
            "tool",
            name=name,
            args=args,
            ms=ms,
            ok=ok,
            error=(res.get("error") if not ok else None),
            # 存全文，不只 head：slim 之后模型看到的就是这份结果，
            # 只存前 400 字符会让"事后重算校验"拿着残缺证据跑，
            # 得出的是另一个题目的答案（实测 cited 从 15 掉到 10 全是截断造成的）。
            result=res_json,
            result_head=res_json[:400],
        )

        slim, cut = _payload(res)
        out_msgs.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": name,
            "content": slim,
        })
        # evidence 存同一份压缩文本：模型看到的和校验层能溯源的必须一致，
        # 存原样会让"模型没读到的数字"也能通过溯源，等于给校验层开后门。
        # truncated 记下来：这条证据被压到装不下，字段可能根本不在里面，
        # 校验时必须把这种情况和"模型引用写错"分开。
        evidence.append({"tool": name, "args": args, "result": slim,
                         "truncated": cut, "turn": int(state.get("turn") or 1)})
        names.append(name)

    return {
        "messages": out_msgs,
        "evidence": evidence,
        # 一轮里 act 可能跑多批，这里合并而不是覆盖，否则只留最后一批
        "tool_names": list(state.get("tool_names") or []) + names,
        "steps": state.get("steps", 0) + len(last["tool_calls"]),
    }













# 取不到值的哨兵。用独立对象而不是 None：字段值本身可能就是 null，
# 两者必须能分开——"字段不存在"和"字段就是空"是两件事。
_MISS = object()


def _load_obj(e):
    """证据对象的原始结构。tool 事件里 result 是 JSON 字符串，也可能已经是 dict。"""
    r = e.get("result")
    if isinstance(r, (dict, list)):
        return r
    if not isinstance(r, str):
        return _MISS
    try:
        return json.loads(r)
    except (ValueError, TypeError):
        return _MISS


# 字段路径切段：字段名或 [下标]。groups[0].rate → ("groups", None) ("rate", None) (None, 0)
_PATH_ATOM = re.compile(r"([A-Za-z_]\w*)|\[(\d+)\]")


def _get_path(obj, path):
    """按 a.b[0].c 取值。任一段不存在就返回 _MISS，不抛异常。"""
    cur = obj
    for m in _PATH_ATOM.finditer(path):
        name, idx = m.group(1), m.group(2)
        if idx is not None:
            if not isinstance(cur, list):
                return _MISS
            i = int(idx)
            if i >= len(cur):
                return _MISS
            cur = cur[i]
        else:
            if not isinstance(cur, dict) or name not in cur:
                return _MISS
            cur = cur[name]
    return cur



def _parse_claims(text):
    """从结论里取出结构化断言。返回 dict 或 None（模型没按契约输出）。

    模型有时会把 JSON 包在 ```json ``` 里，先剥掉再解析。
    剥不掉、解析失败、没有 claims 字段，一律返回 None，
    由调用方退回旧的文本解析路径——契约迁移期不能因为一次格式抖动就丢结论。
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except Exception:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("claims"), list):
        return None
    return d


def _value_match(v, got, unit, tol=0.02):
    """claim 里写的数值和字段实际值是否一致。

    百分比字段通常存小数（0.1541），结论写的是 15.41%——
    这条换算必须在这里做，否则每个百分比都会被判成"引用写错"。
    容差按相对值给：58.82 和 0.588235 这种四舍五入差异不该算错。

    字段值是整段文字时（工具返回的评估说明常这样：{"assessment":
    "已超额定寿命 620 小时，上升 10%-25%"}），没法做等值比较，
    只能比"这段文字里有没有这个数"。这不比数值相等弱——模型引的字段里
    确实有这个数，是返回结构本身没有把它拆成独立字段。
    """
    try:
        t = float(v)
    except (TypeError, ValueError):
        return False
    if isinstance(got, str):
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", got)]
        return any(abs(n - t) <= max(tol, abs(t) * 0.01) for n in nums)
    try:
        g = float(got)
    except (TypeError, ValueError):
        return False
    if unit == "%" and 0 < g <= 1:
        g *= 100
    return abs(g - t) <= max(tol, abs(t) * 0.01)


def _render_claims(doc):
    """把结构化结论渲染成人话，供 UI 显示和评测关键词匹配。

    ref 为空的数字后面补 [无出处]——这是"标灰"唯一真正落地的地方：
    标记必须跟着数字走，只记在报告里等于没标。
    """
    parts = []
    concl = str(doc.get("conclusion") or "").strip()
    if concl:
        parts.append(concl)
    for c in doc.get("claims") or []:
        if not isinstance(c, dict):
            continue
        t = str(c.get("text") or "").strip()
        if not t:
            continue
        mark = "" if c.get("ref") else "[无出处]"
        parts.append(f"- {t}{mark}")
    for a in doc.get("advice") or []:
        a = str(a).strip()
        if a:
            parts.append(f"* {a}")
    return "\n".join(parts)


def _verify_claims(doc, ev, stats, blocked, misquoted):
    """按 claim 逐条校验：一个数字一条，各自带 ref。

    这条路把「引用」从文本解析搬到结构字段，所以三类老毛病结构性消失：
      span      一句三个数字共用一个锚点 —— 想写三个就必须写三条 claim
      写错/没写 混在同一个解析失败里 —— ref 空是没写，ref 指不到是写错
      标灰看不见 只能记在报告里     —— 渲染时把标记跟在数字后面
    """
    for c in doc.get("claims") or []:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        unit = str(c.get("unit") or "").strip()
        v = c.get("value")
        if v is None:
            continue          # 纯论述型 claim，没有数字就不用溯源
        stats["qty"] += 1
        if unit == "%":
            stats["pct"] += 1

        ref = c.get("ref")
        if not ref:
            stats["unsourced"] += 1
            blocked.append(text or str(v))
            continue

        m = re.match(r"^\s*(\d{1,2})#(.+?)\s*$", str(ref))
        if not m:
            stats["ref_bad"] += 1
            misquoted.append(f"{text}（出处格式错:{ref}）")
            continue
        k, path = int(m.group(1)), m.group(2)

        if not (1 <= k <= len(ev)):
            stats["cite_oob"] += 1
            blocked.append(f"{text}（第{k}条不存在）")
            continue

        obj = _load_obj(ev[k - 1])
        if obj is _MISS:
            # 不该发生：_payload 已保证输出永远是合法 JSON（超限时降级成
            # _payload_overflow 包装对象）。真到了这里说明有别处写坏了证据。
            stats["ref_bad"] += 1
            misquoted.append(f"{text}（第{k}条不是结构化返回）")
            continue

        got = _get_path(obj, path)
        if got is _MISS:
            # 证据被压缩装不下时，字段本身可能就没送进来。这时模型写的路径
            # 也许完全正确，只是那条证据里已经没有它 —— 判模型写错是误判。
            # 和"字段真不存在"分开：前者放行标灰，后者判失败。
            if ev[k - 1].get("truncated"):
                stats["cite_cut"] += 1
                blocked.append(f"{text}（第{k}条返回体过大已压缩，该字段未保留）")
            else:
                stats["path_miss"] += 1
                misquoted.append(f"{text}（字段不存在:{path}）")
            continue

        if _value_match(v, got, unit):
            stats["path_ok"] += 1
            stats["cited_path"] += 1
            stats["cited_ok"] += 1
        else:
            stats["path_bad"] += 1
            misquoted.append(f"{text}（字段值 {got} ≠ {v}{unit}）")

    stats["cited"] = stats["cited_ok"]
    stats["ev"] = len(ev)



def verify(state):
    """程序级校验：结论里的每个数量必须能被它引用的那条证据支撑。

    判定分四层：
      cited_ok   标了 [k] 且 k 那条证据里确实有  → 通过
      cited_bad  标了 [k] 但 k 那条证据里找不到  → 硬拦（明确编造）
      cite_oob   [k] 越界（多为上下文裁剪所致）  → 记为未引用，不判死
      uncited    没标角，退回全证据宽口径        → 通过但记为未引用

    为什么分层：prompt 遵从率会波动，若把"漏标"也一上来判成编造，
    会造成大面积误报；而误报比漏报更伤——一旦频繁误报，人就会忽略告警。

    覆盖面也扩了：原来只校验 %，一条 420 字的告警解释结论 pct_count=0，
    等于一个数都没看却显示"数字全部可溯源"。现在校验百分比 + 带单位的量，
    设备号 / 告警码 / 日期属于标识符，刻意排除。
    """
    # lookup / knowledge 直接产出 final，不经过 messages；诊断路径才从 assistant 消息取
    final = (state.get("final") or "").strip()
    if not final:
        msgs = state.get("messages") or []
        if msgs and msgs[-1].get("role") == "assistant":
            final = (msgs[-1].get("content") or "").strip()
    no_final = False
    if not final:
        names = ", ".join(state.get("tool_names", []))
        final = f"（已达取证步数上限，已执行工具：{names}，未能形成完整结论）"
        no_final = True

    # 溯源只用本轮和上一轮的工具返回：更早的证据已经被挤出上下文窗口，
    # 模型本轮根本没读到，拿它兜底等于给未溯源数字开后门。
    # 下界必须来自 _ctx_window，和 _context_messages 保留的轮数同源——
    # 这里原本写死 cur_turn - 1，与 CTX_VISIBLE_TURNS=2 数值巧合相等，
    # 改任一处就会让模型的 k 与 ev 的下标整体错位。
    cur_turn = int(state.get("turn") or 1)
    lo, _ = _ctx_window(cur_turn)
    ev = [e for e in (state.get("evidence") or [])
          if int(e.get("turn") or 1) >= lo]

    blocked = []
    # 未溯源 ≠ 伪引用。两个列表装的是两类东西：
    #   blocked    ref 写成 null —— 模型明说"这个数没有直接出处"，标灰放行
    #   misquoted  ref 指到了字段、但那个字段撑不住这个数 —— 判失败
    # 前者可能是算出来的、数出来的、或自己定的目标，程序无从判断，不该判死；
    # 后者是明确声称了出处又对不上，性质不同，混在一起等于判同一个刑。
    misquoted = []
    stats = {"qty": 0, "pct": 0,
             "cited_ok": 0,          # 引用成立（= path_ok，保留旧名给汇总用）
             "cited_path": 0,        # 走的是字段路径
             "cite_oob": 0,          # ref 指向不存在的第 k 条（多为上下文裁剪所致）
             "path_ok": 0, "path_bad": 0, "path_miss": 0,
             # json_bad  模型没按 claims 契约输出 → 整条无法核验，判失败
             # unsourced claim 的 ref 写了 null（有数字、没出处）
             # ref_bad   ref 格式错，或引用的那条证据不是结构化返回
             # cite_cut  那条证据返回体过大被压缩，字段没保留 → 放行标灰
             "json_bad": 0, "unsourced": 0, "ref_bad": 0, "cite_cut": 0}

    # 结构化契约：结论必须是 claims JSON。引用是字段，不是文本里的字，
    # 所以不用再去正则里猜"这个数属于哪个锚点"。
    doc = _parse_claims(final)
    if doc is not None:
        _verify_claims(doc, ev, stats, blocked, misquoted)
        final_text = _render_claims(doc)
        trace.emit("verify", blocked=blocked, misquoted=misquoted,
                   final_len=len(final), **stats)
        return {"final": final, "final_text": final_text,
                "blocked": blocked, "misquoted": misquoted, "cite_stats": stats}

    # 没按契约输出。这里不放行：放行等于"这条没校验"，
    # 而没校验的结论被当成通过，才是真正危险的地方。
    # 唯一例外是步数用尽、压根没形成结论那条，它本来就没声明要溯源。
    stats["json_bad"] = 1
    if not no_final:
        misquoted.append("结论未按 claims 契约输出，引用无法核验")
    trace.emit("verify", blocked=blocked, misquoted=misquoted,
               final_len=len(final), **stats)
    return {"final": final, "final_text": final, "blocked": blocked,
            "misquoted": misquoted, "cite_stats": stats}


def _pct(v, digits=2):
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(v)


def _data_block(prof, log):
    lines = [
        f"### {prof['device_id']}（{prof.get('line', '-')} / {prof.get('station', '-')}）",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 相机 | {prof.get('camera_model', '-')} |",
        f"| 镜头 | {prof.get('lens_model', '-')} |",
        f"| 光源 | {prof.get('light_model', '-')} |",
        f"| 安装日期 | {prof.get('install_date', '-')} |",
        f"| 保养周期 | {prof.get('maintenance_cycle_days', '-')} 天 |",
        f"| 日志条数 | {prof.get('log_count', '-')}（最新 {prof.get('latest_log_at', '-')}） |",
    ]
    if isinstance(log, dict) and "error" not in log:
        lines += [
            "",
            f"最近 7 天（{log.get('time_from', '-')} ~ {log.get('time_to', '-')}）："
            f"共判定 {log.get('total', 0)} 条，过杀 {log.get('false_positive_count', 0)} 条，"
            f"整体误判率 {_pct(log.get('false_positive_rate', 0))}",
            "",
            "| 工位 | 判定 | 过杀 | 误判率 |",
            "|---|---|---|---|",
        ]
        for g in (log.get("groups") or [])[:8]:
            lines.append(
                f"| {g['key']} | {g['count']} | {g['false_positive']} | {_pct(g['rate'])} |"
            )
    return "\n".join(lines)


def lookup(state):
    """只读查询：把数据直接给出来，不做根因判断。

    刻意不让模型组织这段文字。之前输入「cx-104 数据」时，模型把「数据」理解成
    「误判分布」并直接给了根因结论——用模板拼能从根上杜绝它替用户补意图。
    """
    ids = (state.get("device_ids") or [])[:3]
    blocks, evidence, names, steps = [], [], [], 0

    turn = int(state.get("turn") or 1)
    for d in ids:
        prof = query_device_profile(d)
        evidence.append({"tool": "query_device_profile", "args": {"device_id": d},
                         "result": prof, "turn": turn})
        names.append("query_device_profile")
        steps += 1
        if isinstance(prof, dict) and "error" in prof:
            known = prof.get("known_devices") or []
            blocks.append(f"### {d}\n\n未查到这台设备。当前库里有：{', '.join(known)}")
            continue
        log = query_defect_log(d, group_by="station")
        evidence.append({"tool": "query_defect_log",
                         "args": {"device_id": d, "group_by": "station"},
                         "result": log, "turn": turn})
        names.append("query_defect_log")
        steps += 1
        blocks.append(_data_block(prof, log))

        # 工单也属于"这台设备的数据"，在这里一并给出来。
        # 之前这里只查档案与误判统计，导致用户问「上次那个工单处理了吗」被判成 lookup 后
        # 只能拿到档案和误判率——明明库里 5 条工单，却答不出工单状态。
        # 不做语义判断、不猜用户是不是想问工单：只要查设备数据，就把工单一起列出来。
        # 刻意不渲染 root_cause 全文：lookup 的合同是"不做根因判断"，
        # 把根因描述摆进给用户的文本里，等于变相给了结论。
        wo = query_work_order(device_id=d)
        evidence.append({"tool": "query_work_order",
                         "args": {"device_id": d}, "result": wo, "turn": turn})
        names.append("query_work_order")
        steps += 1
        if wo.get("state") == "found":
            lines = "\n".join(
                f"- {o['order_id']} | {o['status']} | 严重度 {o['severity']} | "
                f"{o['assignee_group']} | 建单 {o['created_at']}"
                for o in wo["orders"]
            )
            blocks.append(f"### {d} 历史工单（{wo['count']} 张）\n\n{lines}")
        else:
            blocks.append(f"### {d} 历史工单\n\n{wo.get('note', '没有工单记录')}")

    body = "\n\n".join(blocks) if blocks else "没有可用的设备编号。"
    tail = (
        "\n\n---\n以上是设备档案、最近 7 天的误判统计与历史工单，**没有做根因判断**。"
        "需要排查原因的话，说一句现象（哪类缺陷、从什么时候开始、哪个班次更重），我再往下查。"
    )
    trace.emit("lookup", device_ids=ids, blocks=len(blocks), steps=steps)
    return {"final": body + tail, "evidence": evidence, "tool_names": names, "steps": steps}


KNOWLEDGE_PROMPT = """你在回答视觉检测设备的规则/标准/含义类问题。

只能使用用户给出的手册片段回答：
1. 每处结论后面用 [编号] 标注出处，编号对应片段编号
2. 片段里没有的内容不要补，宁可说"手册中未收录"
3. 如果不同片段对同一参数给出不同数值，必须指出这是冲突并列出各出处，不要自己挑一个
4. 不要编造任何数字"""


def knowledge(state):
    """知识查询：查手册并回答，明确标注出处。

    这里刻意实现「三态」——检索服务不可用 / 没检索到 / 检索到了，
    三种情况三种回答，避免模型把「服务挂了」说成「手册里没有」。
    """
    q = state.get("user_msg", "")
    names, evidence, steps = [], [], 0

    try:
        res = search_manual(q, top_k=5)
    except Exception as exc:
        trace.emit("knowledge", status="service_unavailable",
                   error=f"{type(exc).__name__}: {exc}")
        return {"final": (
            f"手册检索服务当前不可用（{type(exc).__name__}: {exc}）。\n\n"
            "注意：这不是「手册里没有」，而是检索本身没跑起来——不要据此推断标准值。"
            "可以稍后重试，或直接查阅 SOP 原文。"
        )}

    names.append("search_manual")
    steps += 1
    evidence.append({"tool": "search_manual", "args": {"query": q}, "result": res,
                     "turn": int(state.get("turn") or 1)})

    if isinstance(res, dict) and "error" in res:
        trace.emit("knowledge", status="tool_error", error=str(res["error"])[:120])
        return {"final": f"检索失败：{res['error']}\n\n这是检索问题，不代表手册里没有这条内容。",
                "evidence": evidence, "tool_names": names, "steps": steps}

    chunks = (res or {}).get("chunks") or []
    if not chunks:
        trace.emit("knowledge", status="empty", chunks=0)
        return {"final": ("手册里没有检索到相关内容。可能是这个说法在文档中不存在，"
                          "换个说法再问；也可能这条规则确实没有写进手册。"),
                "evidence": evidence, "tool_names": names, "steps": steps}

    # 问的是参数取值 → 必须查跨文档冲突（L3 的核心，不能只读检索第一条就照抄）
    conflicts = []
    for p in (state.get("param_names") or [])[:3]:
        try:
            c = check_doc_conflict(p)
        except Exception as exc:
            c = {"error": f"{type(exc).__name__}: {exc}"}
        names.append("check_doc_conflict")
        steps += 1
        evidence.append({"tool": "check_doc_conflict", "args": {"param_name": p},
                         "result": c, "turn": int(state.get("turn") or 1)})
        if isinstance(c, dict) and c.get("has_conflict"):
            conflicts.append(c)

    conflict_note = ""
    if conflicts:
        rows = []
        for c in conflicts:
            vals = "；".join(f"{x.get('source', '?')}={x.get('value')}" for x in c.get("conflicts", []))
            rows.append(f"- 参数「{c.get('param_name', '?')}」取值不一致：{vals}")
        conflict_note = (
            "\n\n[系统核查结果] 下列参数在不同文档中取值不一致，必须在回答里明确指出冲突并列出各出处，"
            "不要自己挑一个当作标准值：\n" + "\n".join(rows)
        )

    ctx = "\n\n".join(
        f"[{i + 1}] 出处：{c.get('source_doc', '?')} / {c.get('section', '?')}\n{c.get('text', '')}"
        for i, c in enumerate(chunks)
    ) + conflict_note

    try:
        resp = get_client().chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": KNOWLEDGE_PROMPT},
                {"role": "user", "content": f"问题：{q}\n\n手册片段：\n{ctx}"},
            ],
            temperature=0,
            timeout=CALL_TIMEOUT,
        )
        answer = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        if trace.current() and usage:
            trace.current().add_tokens(
                (getattr(usage, "prompt_tokens", 0) or 0)
                + (getattr(usage, "completion_tokens", 0) or 0)
            )
    except Exception as exc:
        answer = (f"（整理答案时调用失败：{type(exc).__name__}）\n\n"
                  f"原始手册片段如下，供人工核对：\n\n{ctx}")

    trace.emit("knowledge", status="ok", chunks=len(chunks), answer_len=len(answer))
    return {"final": answer, "evidence": evidence, "tool_names": names, "steps": steps}


def finalize(state):
    """收尾，并给本轮写一句话摘要。

    摘要用规则拼、不再调一次模型：可复现、不花钱、出问题能定位。
    上下文窗口裁剪掉的轮次，就靠这段文字留在 system prompt 里。
    """
    turn = int(state.get("turn") or 1)
    tools = ",".join(state.get("tool_names") or []) or "无"
    user_head = (state.get("user_msg") or "").replace("\n", " ")[:50]
    final_head = (state.get("final") or "").replace("\n", " ")[:80]
    line = (f"\n[{turn}] 用户「{user_head}」→ {state.get('intent') or '?'}；"
            f"工具 {tools}；结论「{final_head}」")
    return {"final": state.get("final", ""), "session_summary": line}
