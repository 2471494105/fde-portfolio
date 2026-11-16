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
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# 引用角标：[1] [2]。只认 1–2 位数字，[2026-11-08] 这类日期不会被当成引用。
CITE_RE = re.compile(r"\[(\d{1,2})\]")

# 路径锚点：[3#maintenance_cycle_days]、[4#groups[0].count]。
# 引用粒度从"第几条证据"降到"哪个字段下一句话里的 90 天出自哪。
# 现在模型想表达"这个 90 天是档案里的维护周期"，只有 [1] 或 [9] 两种写法；
# 一句话里三个数字来自三条证据时，句末只能标一个角标——
# 90天[9] 就是这么引错的，不是模型疏忽，是引用语法本身不够用。
#
# 为什么不用正则：[4#groups[0].count] 的路径自带方括号，
# 任何 [^\]]+ 都会在 groups[0 处提前截断（实测就是这个 bug）；
# 而且 [0] 会被 CITE_RE 顺手当成角标 0。所以改成手写扫描 + 把锚点从句子里剥掉。
_PATH_CH = set("_.-")


def _split_anchor(text):
    """把路径锚点从句子里剥掉，返回 (纯文本, [(k, path)])。

    剥掉是因为路径里的 [0] 会被当成引用角标、里面的数字会被当成数量。
    """
    out, kept, i, n = [], [], 0, len(text)
    while i < n:
        lb = text.find("[", i)
        if lb < 0:
            break
        j, s = lb + 1, lb + 1
        while j < n and text[j].isdigit() and j - s < 2:
            j += 1
        if j > s and j < n and text[j] == "#":
            k = int(text[s:j])
            j += 1
            depth, pstart = 0, j
            while j < n:
                ch = text[j]
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    if depth == 0:
                        break
                    depth -= 1
                elif not (ch.isalnum() or ch in _PATH_CH):
                    break
                j += 1
            if j > pstart and j < n and text[j] == "]":
                kept.append(text[i:lb])
                out.append((k, text[pstart:j]))
                i = j + 1
                continue
        kept.append(text[i:lb + 1])
        i = lb + 1
    kept.append(text[i:])
    return "".join(kept), out

# 需要溯源的量：百分比 + 带单位的数值。
# 设备号 CX-104、告警码 E-101、日期 2024-08-15 都是标识符而非论据，刻意排除——
# 之前只校验 %，一条 420 字的告警解释结论 pct_count=0，等于一个数都没看，
# 却照样显示"数字全部可溯源"。
# "个" 必须排除"个百分点"："只差 1.37 个百分点"里的 1.37 不是计数，
# 但 (\d+)\s*(个) 会从"个"字里切出一个假的数量来。实测在 H2 上误拦过一条。
_QTY_UNIT = r"天|小时|分钟|秒|mm|条|台|次|(?:个(?!百分点))|块|倍|工位"
QTY_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*(%|{_QTY_UNIT})")

# 证据里的日期只认完整年月日。MM-DD 无法判断是否跨年，YYYY/MM/DD 这类写法库里也不出现
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# 显式算式：74%（=20/27）或 74%(20/27)。
# 要求写算式，是因为"靠凑"根本不可信——单条证据 135 个数字时，
# 任意编造的百分比有 76% 能被两个数相除凑出来，这条通道等于白送。
EXPR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*[（(]\s*=?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)"
)

# 倍率算式：1.4倍（=0.42/0.3）。
# 原来只有百分比算式被认，模型写"倍"时同样规范、同样给了分子分母，却被当成编造拦下
# （实测一次真实诊断就拦了 1 个）。算式本身是最强的溯源证据——模型主动交代了怎么算的。
EXPR_MULT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*倍\s*[（(]\s*=?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)"
)

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
10. 结论里每个数字后面要标引用编号 [k]，能定位到具体字段就写成 [k#字段路径]。
   k 表示这个数出自第 k 条工具返回，按你实际看到的顺序从 1 开始数。
   字段路径照工具返回的 JSON 原样写：顶层 [1#maintenance_cycle_days]、
   数组下标 [4#groups[0].count]、嵌套 [4#groups[0].rate]。写法：
   - 单个字段：标定周期 90 天[1#maintenance_cycle_days]
   - 一句话里多个数字来自不同字段：整体误判率 15.4%[3#false_positive_rate]、
     1 号工位判定 62 条[3#groups[0].count]
   - 你自己的算出来的比值必须写出算式：74%（=20/27）[1]，分子分母都要在引用的那条证据里。
     校验层会复算，凑不出算式的数字按编造处理。
   工具返回里没有对应字段的数（自己加总出来的、自己定的验收目标），照原样标 [k] 就行，
   不要硬编一个不存在的路径——标错路径比不标更糟。

回答用中文，先说结论（根因 + 证据），再说处置建议。
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


MAX_CTX_TURNS = 2


def _context_messages(state):
    """送给模型的上下文窗口。

    为什么要裁：每轮 messages 会追加 user/assistant/tool 若干条，单条 tool 结果最多 4000 字。
    不裁的话 10 轮之后一次 plan 的 prompt 能到几万 token，成本和延迟都不可接受。

    裁剪单位是「轮」不是「条」：从 user 消息处切，保证 assistant.tool_calls 和紧随其后的
    tool 消息不被切散——切散了 OpenAI 直接报错（tool 消息找不到对应的 tool_call_id）。
    """
    msgs = list(state.get("messages") or [])
    starts = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    if len(starts) <= MAX_CTX_TURNS:
        return msgs
    cut = starts[-MAX_CTX_TURNS]
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


def _slim_result(obj, depth=0):
    """压缩送进模型的工具返回。

    为什么不直接 [:4000] 硬截：json 被截断会切掉括号，模型读到的是一段坏 JSON，
    比读少一点更糟；而且 defect_log 的 groups 数组里，前面几条往往是正常工位，
    关键异常在后面，按位置截会正好把要的证据扔掉。

    这里按结构压：数组留前 8 项并注明总条数，长字符串截 300，标量原样保留。
    """
    if depth > 3:
        return str(obj)[:80]
    if isinstance(obj, dict):
        return {k: _slim_result(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_slim_result(v, depth + 1) for v in obj[:8]]
        if len(obj) > 8:
            items.append(f"...共 {len(obj)} 项，仅送前 8 项")
        return items
    if isinstance(obj, str):
        return obj[:300]
    return obj


def _payload(res, limit=1600):
    """统一出口：模型看到的文本 与 evidence 存的，必须是同一份。"""
    text = json.dumps(_slim_result(res), ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + "…（已截断）"
    return text


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

        slim = _payload(res)
        out_msgs.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": name,
            "content": slim,
        })
        # evidence 存同一份压缩文本：模型看到的和校验层能溯源的必须一致，
        # 存原样会让"模型没读到的数字"也能通过溯源，等于给校验层开后门
        evidence.append({"tool": name, "args": args, "result": slim,
                         "turn": int(state.get("turn") or 1)})
        names.append(name)

    return {
        "messages": out_msgs,
        "evidence": evidence,
        # 一轮里 act 可能跑多批，这里合并而不是覆盖，否则只留最后一批
        "tool_names": list(state.get("tool_names") or []) + names,
        "steps": state.get("steps", 0) + len(last["tool_calls"]),
    }


def _recomputable(v, nums, tol=1.0):
    """结论里的数量能不能由同一份证据里的两个数算出来（a/b * 100 ≈ v）。

    模型常会做"1号工位占全部过杀的比例"这类衍生计算。算法对、但没写计算式，
    严格说不可验证；可从工程上，把它全判成编造会造成大量误报，
    而校验层的误报比漏报更伤——一旦频繁误报，人就会忽略告警。

    关键约束：nums 只传**单条证据**的数字，不再是全部证据混池。
    之前所有证据的数放在一个池子里两两相除，300 个随机编造值有 41% 能被"凑"出来，
    校验层因此形同虚设。
    """
    counts = [n for n in nums if 1 < n < 10000]
    for a in counts:
        for b in counts:
            if b and abs(a / b * 100 - v) <= tol:
                return True
    return False


def _dates_of(text):
    """抽出文本里所有完整日期（YYYY-MM-DD），去重后按时间排序。"""
    out = []
    for y, m, d in _DATE_RE.findall(text or ""):
        try:
            out.append(datetime.date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return sorted(set(out))


def _day_gap_hit(v, text, tol=0.05):
    """结论里的"X 天"能不能由这条证据里**相邻两次记录**的日期相减得到。

    为什么单列这一类：诊断结论几乎每条都会算"两次标定间隔 36 天""两次告警间隔"，
    两个日期都在证据里、差值也算得对，但原来的校验只认"数字直接出现"和
    "a/b 算百分比"两种溯源，于是这类合法推导被系统性误拦——实测一次真实诊断
    被拦下 3 个数字，全是天数差。

    为什么只允许相邻两天、不放开任意两两：
    query_calibration 一次返回 10 条记录，10 个日期两两组合是 45 个差值，
    覆盖 0~90 天近乎每两天一个取值，编造任意天数都能"凑"上，等于开后门。
    相邻差只有 9 个，且语义恰好就是模型要表达的"两次记录的间隔"。
    """
    ds = _dates_of(text)
    for a, b in zip(ds, ds[1:]):
        if abs((b - a).days - v) <= tol:
            return True
    return False


def _expr_ok(ex, v, unit, nums):
    """句子里写了算式，且算式真的成立、分子分母都能在给定数字里找到。

    nums 传"本次判定允许的那批数字"，不是无脑全池：
    分子分母经常分别来自两条证据（0.42mm 来自标定记录、0.3mm 来自手册），
    要求必须在同一条证据里凑齐，会把这种规范写法判成编造；
    但也不能退到全池——无角标时退回全池已是既有口径，有角标时只用被引用的那几条。

    和"靠两个数相除凑出百分比"的区别：那条不需要模型说明，属于弱溯源，只放行不计数；
    这条要求**模型自己写出 a/b**，是最强的证明，计入引用。
    """
    if not ex:
        return False
    a, b = ex
    if not b:
        return False
    if unit == "%":
        if abs(a / b * 100 - v) > 0.5:
            return False
    elif abs(a / b - v) > 0.05:
        return False
    return _hit(a, nums, ratio_ok=False) and _hit(b, nums, ratio_ok=False)


def _lists_of(obj):
    """递归收集 JSON 里所有列表的长度。"""
    found = []
    if isinstance(obj, list):
        found.append(len(obj))
        for x in obj:
            found.extend(_lists_of(x))
    elif isinstance(obj, dict):
        for x in obj.values():
            found.extend(_lists_of(x))
    return found


def _count_hit(v, text):
    """结论里的"X 次 / X 条 / X 个"能不能由这条证据里某个列表的长度得到。

    证据返回的多是记录数组（标定记录、缺陷日志、分组结果）。模型说
    "最近 5 次标定全部通过"时，那个 5 不是抄来的，是数数组长度数出来的，
    文本里根本没有"5"。只认计数单位——"5mm""5 天"用数组长度解释毫无意义。

    收紧点：数值必须是整数、且**恰好等于**某个列表长度。
    不取"≤ 长度"也不取"分组数"，否则 10 个分组就能让"10 条"蒙混过关。
    """
    if not float(v).is_integer():
        return False
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return False
    return int(v) in _lists_of(obj)


def _hit(v, nums, ratio_ok=True, tol=0.05, ratio_tol=0.001):
    """直接命中：证据里就有这个数（含"证据存比率、结论写百分比"两种口径）。

    两条通道都必须收得很紧，否则"数字密集的证据 + 宽容差"等于没检查：
    - 数值通道 tol=0.05：实测在 135 个数字的单条证据里，tol=0.5 能让 27.8% 的随机值
      "命中"（只有 11 个数字的证据也有 12.4%）；收到 0.05 后降到约 2.8%。
    - 比率通道 ratio_tol=0.001：证据存 0.632、结论写 63.2% 是正常的两种口径。
      原来的 0.01 会让 91.7%（v/100=0.917）撞上证据里的 0.92 就算命中——
      数字一密，任何百分比都能找到"一半的邻居"。
    - ratio_ok=False 用于绝对量：9200 小时的 v/100=92 不该因为证据里有个 92 就算命中。
    """
    for n in nums:
        if abs(n - v) <= tol:
            return True
        if ratio_ok and abs(n - v / 100) <= ratio_tol:
            return True
    return False


def _found(v, unit, nums):
    """这个数量能不能在这批数字里找到出处。

    百分比允许复算（模型常做 a/b 的衍生计算，例如 20/27 写 74%）；
    其它单位（天 / mm / 条 / 小时 …）只认直接命中：
    "5mm" 这种绝对量靠两个数相除凑出来毫无业务意义，
    实测 105/2024*100 ≈ 5.19 就能把一个证据里根本没有的 5mm 放行。
    """
    if _hit(v, nums, ratio_ok=(unit == "%")):
        return True
    return unit == "%" and _recomputable(v, nums)


def _nums_of(text):
    # 不带 -? ：否则 "10%-25%" 会被抽成 -25，把合法的 25% 判成未溯源
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text or "")]


def _ev_text(e):
    r = e.get("result")
    return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, default=str)


# 路径解析用的哨兵：路径走不通时返回它，调用方据此区分"没有这个字段"和"值是 None"。
_MISS = object()
_PATH_ATOM = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


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


def _path_probe(v, k, path, unit, ev):
    """路径锚点探测。返回 (类型, 值是否相等)。

    类型分三种，因为"锚点没命中"背后是三种完全不同的原因，
    混成一个计数就看不出模型到底错在哪：
      ok    字段存在且是标量
      miss  路径上某一段不存在（字段名或下标写错）
      arr   锚点指向对象/数组（模型想引用整组记录，语法还不支持）
      none  本次判不了（证据取不到、result 不是 JSON），一律不判死
    """
    if not (1 <= k <= len(ev)):
        return "miss", False
    obj = _load_obj(ev[k - 1])
    if obj is _MISS:
        return "none", False
    got = _get_path(obj, path)
    if got is _MISS:
        return "miss", False
    if isinstance(got, (list, dict)):
        return "arr", False
    if isinstance(got, bool) or not isinstance(got, (int, float, str)):
        return "none", False
    try:
        gv = float(got)
    except (TypeError, ValueError):
        return "none", False
    # 率类字段存的是小数（false_positive_rate=0.154），模型写的是 15.4%。
    # 不换算的话百分比会全军覆没，统计就失去意义。
    cands = [gv]
    if unit == "%" and 0 < gv <= 1:
        cands.append(gv * 100)
    ok = any(abs(c - v) <= max(abs(v) * 0.001, 1e-9) for c in cands)
    return "ok", ok


def _sentences(text):
    """按句切。引用角标是句级的：模型不会把 [1] 贴在每个数字后面，
    而是写在句末（"标定周期 90 天。[1]"），所以数字按所在句子认领角标。"""
    return re.split(r"[。；\n|]", text)


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
    if not final:
        names = ", ".join(state.get("tool_names", []))
        final = f"（已达取证步数上限，已执行工具：{names}，未能形成完整结论）"

    # 溯源只用本轮和上一轮的工具返回：更早的证据已经被挤出上下文窗口，
    # 模型本轮根本没读到，拿它兜底等于给未溯源数字开后门
    cur_turn = int(state.get("turn") or 1)
    ev = [e for e in (state.get("evidence") or [])
          if int(e.get("turn") or 1) >= cur_turn - 1]
    ev_texts = [_ev_text(e) for e in ev]
    # 角标 k 指向 ev 的第 k 条（1 起），与模型在 messages 里看到的顺序一致
    all_nums = _nums_of(" ".join(ev_texts))
    # 用户自己给出的数字也算合法来源（W0 踩过：用户输入的设备号被当成编造）
    all_nums += _nums_of(state.get("user_msg", ""))

    blocked = []
    stats = {"qty": 0, "pct": 0, "cited_ok": 0, "cited_bad": 0,
             "cite_oob": 0, "uncited": 0, "recomputed": 0, "derived": 0,
             "counted": 0,
             # 路径锚点的观测指标。原先全塞进一个 path_bad，看不出模型错在哪：
             # miss=字段/下标写错，arr=指向整组记录（语法还没支持），
             # span=字段都在但这个数不是它们的值（多为同句多数字只标一个锚点）。
             "path_total": 0, "path_ok": 0, "path_bad": 0,
             "path_miss": 0, "path_arr": 0, "path_span": 0,
             # 引用来源拆开：direct 是角标 [k]，path 是 [k#path]。
             # 原来混在 cited_ok 里，报"引用 23 个"却说不清是靠哪种引用的。
             "cited_direct": 0, "cited_path": 0,
             # 该锚未锚：句中有锚点语法可用，但这个数既没被锚点覆盖、也不在算式里。
             "missed_anchor": 0}

    for sent in _sentences(final):
        # 先剥锚点再抽数字：路径里的 [0] 和字段名里的数字都不该参与判定。
        sent_body, path_ks = _split_anchor(sent)
        stats["path_total"] += len(path_ks)
        pairs = QTY_RE.findall(sent_body)
        if not pairs:
            continue
        ks = [int(k) for k in CITE_RE.findall(sent_body)]
        # 同一句可能有多个数字多个锚点，所以逐个数字去试。
        # 同一句里写了的显式算式，按 (单位, 结果值) 索引。
        # 带上单位是因为 "%" 和 "倍" 的算式形态不同（a/b*100 与 a/b），不能混用。
        exprs = {}
        for m in EXPR_RE.finditer(sent_body):
            exprs[("%", float(m.group(1)))] = (float(m.group(2)), float(m.group(3)))
        for m in EXPR_MULT_RE.finditer(sent_body):
            exprs[("倍", float(m.group(1)))] = (float(m.group(2)), float(m.group(3)))

        for num, unit in pairs:
            v = float(num)
            stats["qty"] += 1
            if unit == "%":
                stats["pct"] += 1

            # 漏锚判定放在路径分支前面：句子里没锚点、这个数又不在算式里，
            # 说明模型压根没尝试用字段锚点，属于遵从度问题，不是通道不够。
            if not path_ks and (unit, v) not in exprs:
                stats["missed_anchor"] += 1

            # 路径锚点优先：模型直接交代了这个数出自哪个字段，比"第几条证据"硬得多。
            # 判不出来（none，例如解析失败）不往下拦，交给原有五条通道——
            # 这一步的目标是量遵从率，不是顺手收紧。
            if path_ks:
                kinds = []
                hit = False
                for pk, p in path_ks:
                    kind, match = _path_probe(v, pk, p, unit, ev)
                    if kind == "ok" and match:
                        hit = True
                        break
                    kinds.append(kind)
                if hit:
                    stats["path_ok"] += 1
                    stats["cited_path"] += 1
                    stats["cited_ok"] += 1
                    continue
                if "miss" in kinds:
                    stats["path_miss"] += 1
                elif "arr" in kinds:
                    stats["path_arr"] += 1
                elif "ok" in kinds:
                    stats["path_span"] += 1
                stats["path_bad"] += 1

            if not ks:
                stats["uncited"] += 1
                if _hit(v, all_nums, ratio_ok=(unit == "%")):
                    continue
                # 没标角标但写了完整算式：照样放行。算式比"数字恰好出现在某条证据里"
                # 更硬——模型主动交代了怎么算的，读者可以自己复核。
                if _expr_ok(exprs.get((unit, v)), v, unit, all_nums):
                    continue
                if unit == "%" and _recomputable(v, all_nums):
                    stats["recomputed"] += 1
                    continue
                blocked.append(f"{num}{unit}")
                continue

            valid = [k for k in ks if 1 <= k <= len(ev_texts)]
            if not valid:
                # 角标越界：多半是上下文裁剪把前面的证据挤掉了，不能算编造
                stats["cite_oob"] += 1
                continue

            scopes = [_nums_of(ev_texts[k - 1]) for k in valid]

            # 强溯源 1：引用的那条证据里直接有这个数
            if any(_hit(v, s, ratio_ok=(unit == "%")) for s in scopes):
                stats["cited_direct"] += 1
                stats["cited_ok"] += 1
                continue

            # 强溯源 2：写了算式且算式成立。分子分母允许分别来自被引用的不同证据——
            # "0.42mm 是限值的 1.4 倍（=0.42/0.3）"里，0.42 来自标定记录、0.3 来自手册。
            if _expr_ok(exprs.get((unit, v)), v, unit,
                        [n for s in scopes for n in s]):
                stats["cited_direct"] += 1
                stats["cited_ok"] += 1
                continue

            # 弱溯源：靠两个数相除凑出来。放行，但不算"引用"——
            # 单条证据 135 个数字时，随机编造的百分比有 76% 能被这样凑出来，
            # 把它算进引用率就等于自己骗自己。
            if unit == "%" and any(_recomputable(v, s) for s in scopes):
                stats["recomputed"] += 1
                continue

            # 推导值：证据里相邻两次记录的日期差。放行，但不算"引用"——
            # 它确实是推出来的，不是抄的，跟 recomputed 同类，各记一个计数。
            # 注意要传原始证据文本（ev_texts），scopes 里是抽好的数字，没有日期结构。
            if unit == "天" and any(_day_gap_hit(v, ev_texts[k - 1]) for k in valid):
                stats["derived"] += 1
                continue

            # 计数：证据里有个长度恰好等于 v 的记录数组。
            # "最近 5 次标定全部通过"里的 5 是数出来的，不是抄的，跟 derived 同类。
            if unit in ("次", "条", "个") \
                    and any(_count_hit(v, ev_texts[k - 1]) for k in valid):
                stats["counted"] += 1
                continue

            stats["cited_bad"] += 1
            blocked.append(f"{num}{unit}[{','.join(str(k) for k in ks)}]")

    stats["cited"] = stats["cited_ok"]
    stats["ev"] = len(ev)
    trace.emit("verify", blocked=blocked, final_len=len(final), **stats)
    return {"final": final, "blocked": blocked, "cite_stats": stats}


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
