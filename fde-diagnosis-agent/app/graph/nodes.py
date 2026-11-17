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
# 判"结论里有没有数字"之前，先把标识符剥掉。
# 第一次真实触发就踩到：手册答"没有 CX-104 这个型号"，结论被判成
# "写了数字但 claims 为空"——104 是型号编号，不是量。
# 这和 verify 早先定的口径一致：设备号 / 告警码 / 日期属于标识符，刻意排除。
_IDENT_RE = re.compile(
    r"[A-Za-z]{1,8}-\d{2,8}(?:-\d{2,8})*"                    # CX-104 / E-101 / WO-20261116-004
    r"|\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"     # 2026-11-14 23:14:56
    r"|\d{2}-\d{2}"                                          # 11-07
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
10. 结论必须输出成 JSON，而且只输出这一个 JSON，不要包 ```json``` 代码块。
   数据结构：
{
  "conclusion": "一句话根因，不要在这里写数字",
  "claims": [
    {"text": "含数字的原话", "value": 90, "unit": "天", "ref": "1#maintenance_cycle_days"},
    {"text": "误判率 58.82%", "value": 58.82, "unit": "%", "ref": "3#groups[0].rate"},
    {"text": "复位后预计降到 6%~11%", "value": 6, "value_max": 11, "unit": "%", "ref": null}
  ],
  "advice": ["先重新标定再复测", "把 11-07 前后的标定记录调出来"]
}
   规则：
   - 结论里每出现一个数字，就单独占一条 claim。三个数字要写三条，
     不许挤在一句话里共用一个 ref——这是最关键的一条。
   - value 只写数字本身：58.82% 写成 58.82，unit 写 "%"；90 天写成 90，unit 写 "天"。
   - 结论里出现区间（「6%~11%」「2–4 条」）时写成 value + value_max 两个数，
     value 是下界、value_max 是上界。
     ref 指着某个具体数值字段时禁止写 value_max：那个位置只担保一个精确值，
     你要说区间就说明这个区间不是它给的，ref 必须写 null。
     ref 指着整段文字字段（原文里本来就写着「上升 10%-25%」）时可以写，
     但两个端点都必须能在那段原文里找到。
   - ref 写这个数的出处：第 k 条工具返回的字段路径，k 按你实际看到的顺序从 1 数。
     字段路径照工具返回的 JSON 原样写，顶层 "2#maintenance_cycle_days"、
     数组下标 "3#groups[0].count"、嵌套 "6#records[0].drift_px"。
   - 自己算出来的值（比值 / 差值 / 加总）ref 写 null，并在 text 里写算式：
     {"text": "74%（=20/27）", "value": 74, "unit": "%", "ref": null}
   - 自己定的验收目标、假设值，ref 也写 null。
   - 没有数字的论述写进 conclusion 或 advice，不要塞进 claims。
   - ref 写错比写 null 更糟：拿不准就不写字段路径，写 null。
   - text / advice / conclusion 里要强调某个词，用中文引号「」，不许用英文双引号。
     外层 JSON 本身就是双引号，你中间插一个裸双引号，字符串当场截断，
     整份结论解析失败——你写对的内容一个也用不上，还全被判成"引用无法核验"。
     实测反例：advice 里写「无法严格量化"夜班vs白班"差异」，
     JSON 在第 2339 字符处断掉，14 条正确的引用全部作废。

回答用中文。conclusion 说根因，claims 放全部带数字的证据，advice 说处置。
"""


def intake(state):
    msg = state.get("user_msg", "")
    m = DEVICE_RE.search(msg)
    device_id = m.group(0).upper() if m else ""
    turn = int(state.get("turn") or 0) + 1
    trace.emit("intake", device_id=device_id, matched=bool(m), msg_len=len(msg), turn=turn)
    # 这几个每轮重写：不清的话上一轮的结论/告警/步数会污染本轮判断。
    # messages 和 evidence 相反，靠 reducer 累积——多轮必须保留。
    # stop_reason 也必须清：lookup/knowledge 这类路径根本不跑 plan，
    # 不清就会把上一轮诊断留下的停止原因，当成本轮的结论报出去。
    return {
        "device_id": device_id,
        "messages": [{"role": "user", "content": msg}],
        "steps": 0,
        "final": "",
        # final_text 也必须清。它是"给人看的那份结论"，verify 里
        # final_text = preset_text or _render_claims(doc) —— 只要上一轮留了值，
        # 这一轮就会被上一轮的文本盖掉，而 final（claims）是本轮的。
        # 三轮探针里就撞上了：轮2 走 knowledge 留了文本，轮3 走 diagnose，
        # 结果 UI 上是"问诊断、答手册"，claims 却全是本轮的、校验还是 ok。
        # 这种错最难发现：指标全绿，只有人看文本才知道答的不是同一件事。
        "final_text": "",
        "blocked": [],
        "tool_names": [],
        "turn": turn,
        "plan_retry": 0,
        "stop_reason": "",
        # 和 stop_reason 同理：这一轮要是走了 lookup 或 clarify（不碰 knowledge），
        # 上一轮留下的 svc_fail 会被 verify 当成本轮的失败，凭空多一条半成品。
        "svc_fail": "",
        # check 的三个字段同样是本轮状态，不是会话状态：
        # check_notes 会被 verify 并进 misquoted（"回环后用 null 换通过"那条），
        # 不清就会让第 1 轮的回环问题在第 2 轮再判一次失败。
        "misquoted": [],
        "cite_stats": {},
        "check_notes": [],
        "check_retry": 0,
        "check_action": "",
        "check_unsourced0": 0,
    }


UNDERSTAND_PROMPT = """你在给一台视觉检测设备诊断系统做第一层意图判断。只做分类，不做诊断，不要调用任何工具。

判断用户这句话属于哪一类：

- diagnose：用户描述了异常现象，想找原因。
  例：误判率高、良率掉了、夜班更差、从某天开始变多、是不是光源导致的
- lookup：用户想看**这台设备**的数据、配置、参数、记录本身，没有要找原因的意思。
  例：给我看下数据、cx-104 的保养周期设的是多少、这台上次标定是什么时候、最近记录
  边界：问的是"某台设备实际怎么配的、上次/现在是多少"，即使问的是参数名，也归 lookup。
  参数名本身是知识，但这台设了多少是数据。
- knowledge：用户在问规则、标准、含义、操作步骤。这类问题**不依赖具体设备**也能回答。
  例：E-101 是什么意思、保养周期规程要求多久、亮度标准值是多少、怎么清洁镜头
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
5. confidence 如实给。两类都解释得通（既像"要数据"又像"要原因"）时给 0.5~0.7，不要凑 0.9。
   系统按它决定要不要先追问一句：你虚报高分会让它跳过追问，带着错的理解往下跑到底。

只输出 JSON，不要任何解释文字：
{
  "intent": "diagnose|lookup|knowledge|action|clarify",
  "confidence": 0.85,
  "device_ids": ["CX-104"],
  "alarm_codes": ["E-101"],
  "param_names": ["光源亮度设定"],
  "phenomenon": "用户描述的异常现象（概括原话），没有就填空字符串",
  "missing": ["还缺什么信息才能继续"],
  "reason": "一句话说明凭什么这么判"
}"""

# 模型判为诊断/查询但输出字段不自洽时的降级说明
_INTENT_ENUM = {"diagnose", "lookup", "knowledge", "action", "clarify"}

# 意图置信闸的两条线。
#
# 低线以下不猜了，直接追问——带着错的理解跑满 8 步是这里最贵的错。
# 高线不取 Jev 那个 0.9：他们的 0.9 是"不可逆决策转人工"（退款、放行），
# 我们这里判错只是多问一句，代价可逆，所以线可以低。
# 实测 9 句的置信分布是 0.60~0.95，取 0.9 会有 6 句都挂免责说明，
# 每句都"我不太确定"等于每句都不确定，用户会免疫。
# 0.75 只在真的两可时才提示——第 8 句「你查一下 cx-104」0.60 会命中，正好。
CONF_ASK = 0.6
CONF_OK = 0.75


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

    # 兜底不能挑最贵的那条路。原来这里是"有设备号就默认 diagnose"，
    # 等于 LLM 一挂就跑满 12 步——把最贵、最容易产出伪结论的路径设成了默认值。
    # lookup 最便宜，而且答非所问时用户一句话就能纠正；clarify 连设备都没有。
    fallback_intent = "lookup" if hint else "clarify"
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

    # ---- 置信度解析（判在硬矛盾之后，避免和降级后的 intent 打架）----
    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        conf = 1.0          # 没给就按高置信走，保持旧行为；给了才闸
    conf = min(max(conf, 0.0), 1.0)
    intent_note = ""

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

    # ---- 置信闸：必须放在硬矛盾校验之后 ----
    # 位置很关键：硬矛盾会改写 intent，闸若在它之前，note 会写着"按 diagnose 理解"
    # 而最终 intent 已经是 clarify，用户看到的是"你说按诊断理解，那你为什么还问我"。
    # 已经降级成 clarify 的不再叠置信说明：追问本身就是不确定性最好的表达。
    # 这层是这次唯一"融合 Jev"的地方——分类器的价值不只在选得准，
    # 还在能报出"我不确定"，并且这个数值真的会被用来改变行为。
    if intent != "clarify" and conf < CONF_ASK:
        intent_note = f"意图置信度 {conf:.2f} 低于 {CONF_ASK}，先追问确认再往下走"
        intent = "clarify"
    elif intent != "clarify" and conf < CONF_OK:
        intent_note = f"意图置信度 {conf:.2f}，按「{intent}」理解，若不对请直接纠正"

    trace.emit(
        "understand",
        intent=intent,
        confidence=round(conf, 3),
        device_ids=ids,
        phenomenon=phen[:120],
        missing=list(data.get("missing") or []),
        reason=reason[:160],
        note=intent_note[:120],
    )

    return {
        "intent": intent,
        "intent_conf": conf,
        "intent_note": intent_note,
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
    """追问。文案按「缺什么」生成，避免模型面对空信息又自己编一个假设。

    判三件事：本轮说了什么（现象）、这台设备确不确定（本轮 ids 或上一轮 slots）、
    意图置信度够不够。已经有的信息不能再让用户说一遍——用户最烦的两句话是
    "你刚才说的我没听见"和"我刚说过的你再问一次"。
    """
    ids = state.get("device_ids") or []
    slots = state.get("slots") or {}
    # 本轮没提设备、但上一轮确认过 → 文案里直接用，不再索要
    known_dev = (ids[0] if ids else "") or (slots.get("device_id") or "")
    phen = (state.get("phenomenon", "") or "").strip()
    phen_show = phen[:30] + ("…" if len(phen) > 30 else "")
    note = (state.get("intent_note") or "").strip()
    missing = [m for m in (state.get("missing") or []) if m]

    if phen and known_dev:
        # 现象和设备都有，只是"查原因"还是"看数据"没定下来
        final = (
            f"{known_dev} 你提到「{phen_show}」。\n\n"
            "- 要排查原因：回一句「查原因」，我取证后给判断\n"
            "- 只想看数据：回一句「看数据」，我直接把最近档案和误判统计给你"
        )
    elif phen and not known_dev:
        # 现象已经有了，只缺设备号——不能说成"需要两个信息"
        final = (
            f"你提到「{phen_show}」，还缺一个设备编号。\n\n"
            "当前库里有 CX-101 ~ CX-105；如果涉及多台，说设备号或工位号都行。"
        )
    elif known_dev and not phen:
        final = (
            f"{known_dev} 你想先看哪一方面？\n\n"
            "- 想排查异常：说一句现象，比如哪类缺陷误判多、从什么时候开始、哪个班次更重\n"
            "- 只想看数据：回一句「看数据」，我直接把最近的档案和误判统计给你"
        )
    else:
        final = (
            "需要两个信息才能开始：\n\n"
            "1. 设备编号（当前库里有 CX-101 ~ CX-105）\n"
            "2. 你想做什么——排查异常、看数据，还是查手册里的规则条款"
        )
    # 低置信降级时，先告诉用户"我是按哪种理解在问"
    if note:
        final = f"{note}\n\n{final}"

    trace.emit("ask_user", intent=state.get("intent", ""), device_ids=ids,
               phenomenon=phen[:80], missing=missing,
               confidence=state.get("intent_conf"),
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


def _visible_evidence(state):
    """参与溯源的证据：只取本轮可见轮次内的，和 _context_messages 同源。

    为什么 check 和 verify 必须都调它：两处各取一份的后果在多轮里很难看——
    check（回环前）拿全量证据，能查到一条模型早就读不到的旧证据，于是放行；
    verify（终局）只拿窗口内的，同一条判 cite_oob，直接 FAIL。
    表现是**回环白跑一轮仍然失败**，而报出来的话像"模型引错了号"。

    check 的存在意义是"提前拦下会让 verify 失败的东西"。两边口径不一致，
    它就只在单轮里有效——单轮下全量等于窗口，看不出差别。
    """
    cur_turn = int(state.get("turn") or 1)
    lo, _ = _ctx_window(cur_turn)
    return [e for e in (state.get("evidence") or [])
            if int(e.get("turn") or 1) >= lo]


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


# 停止原因：本轮"为什么继续/为什么停"的唯一表达。
#
# 为什么要有这个字段：改之前，"这次运行结束了"在代码里有三条来路——
#   步数到 12 强制 finalize、模型没发 tool_calls、plan_retry 触发退一次。
# 三条都汇进同一个出口，产生的报告长得一模一样。于是一次「12 步没结论、
# 被预算掐断」的运行，和一次「3 步数据够了、干净收工」的运行，在报告上
# 都是 OK / 步数 12 / 语义 1/1，可信度却完全不同，事后无法归因。
#
# 取值对齐 Anthropic Messages API 的 stop_reason 语义：
#   need_more        ~ "tool_use"   模型还要调工具，循环继续
#   enough           ~ "end_turn"   模型不再调工具，正常收工
#   budget_exhausted ~ "max_tokens" 还想继续但额度已尽，结论是半成品
#
# 关键一条：budget_exhausted 只在「模型还想继续 + 额度已尽」时成立，
# 不是「步数满 12」就成立。步数满了但模型自己说够了，仍然是 enough——
# 这正是 Anthropic 把 end_turn 和 max_tokens 分成两个值的理由：
# 一个是被截断，一个是被完成，混成一个值就再也分不出来。
#
# 这个值从响应结构推导，不额外让模型声明，也不多花一次调用：
# Anthropic 服务端的 stop_reason 同样是从响应里有没有 tool_use 块读出来的，
# 不是模型自己写一句"我要停了"。
STOP_NEED_MORE = "need_more"
STOP_ENOUGH = "enough"
STOP_BUDGET_EXHAUSTED = "budget_exhausted"


def _classify_stop(has_tool_calls, steps, max_steps=MAX_STEPS):
    """本轮停止原因。纯推导，不调模型、不花 token。"""
    if has_tool_calls:
        return STOP_BUDGET_EXHAUSTED if steps >= max_steps else STOP_NEED_MORE
    return STOP_ENOUGH


def _plan_notes(state, used):
    """本轮追加到 messages 末尾的动态提示，外加要写回的 retry 位。

    为什么单独抽成纯函数：这几行文字决定了模型这一轮看到什么，
    是它判断"继续查还是收工"的全部依据。原来内联在 plan 里，
    写错、漏传都只有真跑一次模型才看得出来——而模型不遵从是静默的，
    事后翻轨迹也找不到"到底把哪几条缺口送进去了"。
    抽出来就能用单测直接断言，不用花钱调模型。

    返回 (notes, gaps, retry)。
    """
    # system prompt 必须逐字恒定，才能命中前缀缓存。
    # 会变的内容（工具预算、跨轮摘要、重试提醒）一律追加到 messages 末尾——
    # 之前把预算拼在 system 开头，步数一涨第一句就变，后面 2000+ token 全部按未命中计费，
    # 实测一条 8 步的用例 5 次 plan 缓存命中率是 0/2265、0/3203、0/4591、0/5265、0/5513。
    notes = [
        f"[工具预算] 已用 {used}/{MAX_STEPS} 次。"
        "判断标准不是「还剩几次」，而是「够不够」：能给出根因和处置就停，不够才继续。"
        "如果用户本轮已明确要求某个动作（例如建单），在根因已明确时优先完成该动作。"
    ]
    # 把 understand 第一轮写下的缺口带回来。
    # 改之前这个字段只写不读：UNDERSTAND_PROMPT 里要求模型填 missing，
    # understand 把它写进 state，ask_user 拿它拼追问文案，trace 记了一笔，
    # 然后 plan 完全不看它。等于模型每轮决策时只知道"用了几次"，不知道"还缺什么"，
    # 停止这件事在 3–9 步这段最贵的区间里没有任何判断依据。
    gaps = [str(g).strip() for g in (state.get("missing") or []) if str(g).strip()]
    if gaps:
        notes.append(
            "[你一开始判断还缺的信息]\n- " + "\n- ".join(gaps)
            + "\n对照上面这些：已经补齐就停止取证、直接给结论；没补齐就只查还缺的那一条。"
        )
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
    return notes, gaps, retry


def plan(state):
    used = state.get("steps", 0)
    sys_prompt = SYSTEM_PROMPT
    notes, gaps, retry = _plan_notes(state, used)

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

    stop = _classify_stop(bool(m.tool_calls), used)
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
        # 停止原因进轨迹：事后复盘"这次为什么停了、是不是被预算掐断的"，
        # 不用再靠步数去猜。
        stop_reason=stop,
        # 这一轮到底把哪几条缺口送给了模型。
        # 不记这一笔，"missing 已经接线"就只是代码里看起来接了——
        # 它有没有真的进到 prompt、模型看没看到，事后无法证明。
        gaps=gaps,
    )
    return {"messages": [out], "plan_retry": retry, "stop_reason": stop}


def route_after_plan(state):
    last = state["messages"][-1]
    has_calls = bool(last.get("tool_calls"))
    # 优先用 plan 刚刚写下的停止原因。plan 是同时返回 messages 和 stop_reason 的，
    # 两者不可能不一致；只有从旧 checkpoint 恢复（state 里还没有这个字段）时才现推导一次。
    stop = state.get("stop_reason") or _classify_stop(has_calls, state.get("steps", 0))

    # 两条结束路径分开走，不再汇进同一个出口：
    #   budget_exhausted → finalize，结论是"预算掐断的半成品"
    #   enough           → check，先按规则自查一遍；不通过就把原因回喂，让它自己改
    # 之前两者都走 verify→finalize，报告上无法区分，一次 12 步没结论的运行
    # 和一次 3 步干净收工的运行长得一模一样。
    if stop == STOP_BUDGET_EXHAUSTED and has_calls:
        return "finalize"
    if stop == STOP_NEED_MORE and has_calls:
        return "act"
    # 剩下的都当"模型不再调工具"。这里仍然按实际消息判断，不信字段：
    # 消息是 act 真正会执行的东西，不能让一个字段把图路由进空转节点。
    # 正文写了"接下来调用 X"但 tool_calls 为空：不认它是结论，退回 plan 一次。
    # plan_retry 已经在 plan 里置位，所以最多退一次，不会死循环。
    if int(state.get("plan_retry") or 0) == 0 and _pending_call(last.get("content")):
        return "plan"
    return "check"


MAX_CHECK_RETRY = 1


def check(state):
    """交出结论前的自查：把"哪条规则、为什么失败"回喂，让模型自己改一次。

    依据是 Claude Agent SDK 对 agent loop 的描述（gather context → take action →
    verify work → repeat），以及同页那句更具体的话：
    "The best form of feedback is providing clearly defined rules for an output,
    then explaining which rules failed and why."

    也就是说 verify 的产出不是一个布尔，是**失败原因**，而且要能回到循环里。
    本项目原来把 verify 放在循环外，判完就 finalize，模型一个字都没看到。
    后果有实测样本：11-17 那次 H1，模型把 2 号工位的 11.39% 和 3 号工位的 4.84%
    抄反了，_verify_claims 准确抓到「字段值 0.1139 ≠ 4.84%」，
    但抓到时已经在终点，只能判失败。同一句话当时回喂过去，它自己能改。

    这里只决定"要不要再跑一轮"，正式统计与渲染仍归 verify：
    在 check 里也落一份正式 stats，同一份数字会被算两次，汇总直接翻倍。
    """
    retry = int(state.get("check_retry") or 0)
    notes = list(state.get("check_notes") or [])
    unsourced0 = int(state.get("check_unsourced0") or 0)
    doc = _parse_claims(state.get("final") or "")
    # 和 verify 同一批证据。check_ev 也一并交出去：两边取的条数一旦不同，
    # "check 放行、verify 判死"就会发生，而它只在不同轮数下才显形。
    used = _visible_evidence(state)

    # 没按契约输出：交给 verify 判，不值得为格式再花一轮——
    # 上一轮就没写出合法 JSON，重跑一次大概率还是写不出。
    if doc is None:
        return {"check_action": "pass", "check_retry": retry,
                "check_notes": notes, "check_unsourced0": unsourced0,
                "check_ev": len(used)}

    blocked, misquoted = [], []
    stats = _new_stats()
    _verify_claims(doc, used, stats, blocked, misquoted)

    # 回环过一次时的唯一判据：不许拿 null 换通过。
    # 指错的 ref 全改成 null，misquoted 清零，但那些数从"指错了"变成"没指"——
    # 后者不报错，却比前者更差。这条不显式记下来，模型就有一条零成本过关的路。
    if retry >= MAX_CHECK_RETRY:
        if stats["unsourced"] > unsourced0:
            notes.append(
                f"回环后未溯源数字从 {unsourced0} 条涨到 {stats['unsourced']} 条："
                f"把指错的 ref 改成 null 不是修正，是把引用问题换成了没引用")
        return {"check_action": "pass", "check_retry": retry,
                "check_notes": notes, "check_unsourced0": unsourced0,
                "check_ev": len(used)}

    if not misquoted:
        return {"check_action": "pass", "check_retry": retry,
                "check_notes": notes, "check_unsourced0": stats["unsourced"],
                "check_ev": len(used)}

    # 有伪引用且还有一次机会：把具体原因写回去。
    # 文案必须给出"改什么"，只说"引用有误"模型只能猜。
    lines = "\n".join(f"- {m}" for m in misquoted)
    feedback = (
        f"[引用未通过]\n{lines}\n"
        "请重新输出完整的 claims JSON：把数字改成字段里的实际值；"
        "如果这个数确实是算出来的、数出来的或预测值，就把 ref 改成 null "
        "并在 text 里保留算式；如果判断是证据不够，就继续调工具补齐，不要硬写结论。\n"
        "把指错的 ref 直接改成 null 不算修正。"
    )
    return {
        "messages": [{"role": "user", "content": feedback}],
        "check_action": "retry",
        "check_retry": retry + 1,
        "check_notes": notes,
        "check_unsourced0": stats["unsourced"],
        "check_ev": len(used),
        # 清掉上一轮的终局痕迹，否则 route_after_plan 会读到旧的 enough 又进来一次
        "final": "",
        "stop_reason": "",
        "plan_retry": 0,
    }


def route_after_check(state):
    """check 自己的决定。

    用 check_action 而不是"看 messages 有没有变长"：后者在模型恰好在 messages
    里留了一条空 content 时会失效，而路由失效的表现是"本该回环的没回环"——
    静默的，只在纠错次数上少一次，不报任何错。
    """
    return "plan" if state.get("check_action") == "retry" else "verify"


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


# 证据编号标签："[证据 5] {...}"。_payload 写到最前面，_load_obj 负责剥掉。
# 用文本前缀而不是往 JSON 里加 "_ev" 字段，是因为工具返回可能是 list 或标量，
# 加字段要么改结构（模型引用的路径全变）、要么加不进去；前缀对所有类型都成立，
# 且完全不影响 JSON 内部的字段路径。
_EV_TAG = re.compile(r"^\[证据 (\d+)\] ")


def _payload(res, limit=1600, ev_no=None):
    """统一出口：模型看到的文本 与 evidence 存的，必须是同一份。

    返回 (text, truncated)。text 永远是**合法 JSON**——这是硬约束，因为
    verify 要用 _load_obj 解析它取字段；一旦不是 JSON，所有引用都会判
    ref_bad，报出来是"模型引用写错"，实际是证据坏了，归因完全反了
    （实测：一条 1606 字的 defect_log 返回就能触发，正确引用全被判失败）。
    —— 除了最前面那截 "[证据 N] " 标签，它由 _load_obj 剥掉，见 _EV_TAG。

    逐档收紧结构；三档都压不住时不再切字符串，改为降级成一个仍可解析的
    包装对象，把能留的内容放 preview 里。模型看到 preview 仍可引用其中的
    数字（_value_match 支持字符串内抽数），校验层也能据此判是"证据截断"
    而不是"模型写错"。

    ev_no 为什么在这里：让模型自己数"这是第几条工具返回"是本次最贵的
    一处不可靠。实测同一份代码、同一条用例连跑两次，语义 25% 与 100%，
    差异全在 k 错位——字段名模型记得极准（cumulative_hours、records[0].
    drift_px、conflicts[0].value、chunks[2].text 四个工具的字段全对），
    位置完全记不住。编号是纯机械的信息，不该交给会记错的组件去数，
    直接印在它读得到的文本最前面，它照抄即可。
    """
    head = f"[证据 {ev_no}] " if ev_no else ""
    budget = limit - len(head)
    for arr, strlen in _SLIM_LEVELS:
        text = json.dumps(_slim_result(res, 0, arr, strlen),
                          ensure_ascii=False, default=str)
        if len(text) <= budget:
            return head + text, False

    # 结构已压到最小仍超限：降级为合法 JSON，明细进 preview
    full = json.dumps(res, ensure_ascii=False, default=str)
    out = {
        "_payload_overflow": True,
        "note": "返回体过大，已省略结构化明细；preview 是原始内容前段，可从中读数",
        "top_keys": list(res.keys())[:12] if isinstance(res, dict) else [],
        "preview": "",
    }
    room = budget - len(json.dumps(out, ensure_ascii=False)) - 8   # 留转义余量
    n = max(0, room)
    out["preview"] = full[:n]
    text = json.dumps(out, ensure_ascii=False)
    while n > 0 and len(text) > budget:     # 转义会撑长，逐步收窄直到不超
        n = int(n * 0.8)
        out["preview"] = full[:n]
        text = json.dumps(out, ensure_ascii=False)
    return head + text, True


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
    # 编号从"已有条数"续，不能从 1 重数：evidence 在 state 里是累加的，
    # act 每次只返回本轮增量。按增量列表长度算，第二轮又会冒出证据 1、2、3，
    # 同一编号对应两条不同证据，by_no 后写覆盖先写，引用旧编号会静默命中新证据。
    base = len(state.get("evidence") or [])

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

        # 编号是绝对号（跨轮一直递增），不是本轮内的序号：
        # 模型引用的 k 必须是它在 messages 里实际看到的那个编号，
        # 而 _ctx_window 裁剪只影响"参与校验的证据集合"，不能重排编号，
        # 否则同一份证据在两轮里会有两个号。
        ev_no = base + len(evidence) + 1
        slim, cut = _payload(res, ev_no=ev_no)
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
                         "truncated": cut, "ev_no": ev_no,
                         "turn": int(state.get("turn") or 1)})
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
    """证据对象的原始结构。tool 事件里 result 是 JSON 字符串，也可能已经是 dict。

    先剥掉 _payload 加的 "[证据 N] " 前缀——它只是给模型看的位置标记，
    不是数据的一部分。不剥掉，json.loads 会失败，所有引用都被判 ref_bad，
    报出来又是"模型引用写错"，而真凶是包装格式。
    """
    r = e.get("result")
    if isinstance(r, (dict, list)):
        return r
    if not isinstance(r, str):
        return _MISS
    m = _EV_TAG.match(r)
    if m:
        r = r[m.end():]
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



_CJK_OPEN = "「"
_CJK_CLOSE = "」"


def _fix_bare_quotes(body):
    """修 JSON 字符串值里未转义的英文双引号。修不动返回 None。

    模型在 text/advice 里想强调一个词时会写成 "夜班vs白班"，外层也是双引号，
    字符串提前闭合，整份 JSON 解析失败。内容一个字都没错，
    代价却是整条结论被判"引用无法核验、语义正确率 0"——
    把格式错误当成内容错误来罚，是最贵的一类误判。

    只动"值内部、不在结构分隔符位置"的那一种：
      在字符串内遇到双引号时，看它跳过空白后是不是 , : } ] 之一。
      是 → 它是收尾引号，原样保留。
      不是 → 说明它是模型手写进文案的，换成中文引号。
    结构一个字节都不动，所以修出来的语义和模型想表达的一致。

    修完仍然要走全部校验（ref 逐条对证据、值逐条比对），
    所以这条路不是放行通道，只是把"格式坏了"和"内容错了"分开。
    """
    out = []
    in_str = False
    opened = 0          # 已替换的开引号次数，用来决定这次用「还是」
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\\":
            out.append(body[i:i + 2])
            i += 2
            continue
        if ch != '"':
            out.append(ch)
            i += 1
            continue
        if not in_str:
            in_str = True
            out.append(ch)
            i += 1
            continue
        j = i + 1
        while j < n and body[j] in " \t\r\n":
            j += 1
        if j >= n or body[j] in ",:}]":
            in_str = False
            out.append(ch)
            i += 1
            continue
        out.append(_CJK_OPEN if opened % 2 == 0 else _CJK_CLOSE)
        opened += 1
        i += 1
    if opened % 2:
        return None     # 只配了一半，不敢猜它想包到哪
    return "".join(out)


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
    body = s[i:j + 1]
    try:
        d = json.loads(body)
    except Exception:
        # 解析失败先试一次「引号修复」，只修格式不动内容。
        # 修出来的对象打上 _repaired，让 verify 能把"救回来几条"记进统计——
        # 静默修复最危险：模型越不守规矩，你越看不见。
        fixed = _fix_bare_quotes(body)
        if fixed is None:
            return None
        try:
            d = json.loads(fixed)
        except Exception:
            return None
        if isinstance(d, dict):
            d["_repaired"] = True
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
        # 编号要跟着这句话走。只记在报告里，用户看到的就是一个没有出处的数字——
        # 而"看得到出处"才是这套契约对用户的全部意义。
        ref = str(c.get("ref") or "")
        m = re.match(r"^\s*(\d{1,3})#", ref)
        if m:
            mark = f"[{m.group(1)}]"
        elif not ref:
            mark = "[无出处]"
        else:
            mark = "[出处格式错]"
        parts.append(f"- {t}{mark}")
    for a in doc.get("advice") or []:
        a = str(a).strip()
        if a:
            parts.append(f"* {a}")
    return "\n".join(parts)


def _new_stats():
    """校验统计的空壳。verify 正式判定用它，check 预判也用它。

    抽出来是因为 check 需要在**不落正式统计**的前提下跑一遍 _verify_claims：
    它只想知道"会不会失败"，判完就丢。两边各写一份默认键，迟早有一边少了
    某个 key，然后在某个分支里 KeyError——而那个分支恰好只在真跑时才进。
    """
    return {"qty": 0, "pct": 0,
            "cited_ok": 0,          # 引用成立（= path_ok，保留旧名给汇总用）
            "cited_path": 0,        # 走的是字段路径
            "cite_oob": 0,          # ref 指向不存在的第 k 条（多为上下文裁剪所致）
            "path_ok": 0, "path_bad": 0, "path_miss": 0,
            # json_bad  模型没按 claims 契约输出 → 整条无法核验，判失败
            # unsourced claim 的 ref 写了 null（有数字、没出处）
            # ref_bad   ref 格式错，或引用的那条证据不是结构化返回
            # cite_cut  那条证据返回体过大被压缩，字段没保留 → 放行标灰
            # json_fixed 结论 JSON 里值内裸引号被修好了，校验照常进行
            "json_bad": 0, "unsourced": 0, "ref_bad": 0, "cite_cut": 0,
            "json_fixed": 0,
            # range_ok   区间 claim，两端都能在 ref 指向的那段原文里找到
            # range_bad  区间 claim 但撑不住：ref 指着数值字段，或端点不在该原文里
            "range_ok": 0, "range_bad": 0,
            # quote_ok   引文型 claim：模型抄的原文确实在 ref 指向的那段里
            # quote_bad  引文型 claim 但抄错了：原文里找不到这句话
            # bare_conclusion  结论里写了数字、claims 为空 → 没有一个数字可核验
            # svc_fail   整理答案那步模型调用失败：形状合法但内容是半成品
            "quote_ok": 0, "quote_bad": 0, "bare_conclusion": 0,
            "svc_fail": 0}


def _norm_quote(s):
    """比对引文时抹掉排版差异，不抹内容。

    抹空白和全角半角是必须的：模型抄中文原文时，空格和标点全半角最容易漂，
    这类漂移不代表"这句话不在原文里"。但只到这一步——连用词、语序都能抹的话，
    那就不叫核对了。
    """
    s = re.sub(r"\s+", "", str(s))
    for a, b in (("，", ","), ("。", "."), ("；", ";"), ("：", ":"),
                 ("（", "("), ("）", ")"), ("“", '"'), ("”", '"'),
                 ("【", "["), ("】", "]")):
        s = s.replace(a, b)
    return s


def _verify_quote(c, text, quote, by_no, stats, blocked, misquoted):
    """引文型 claim：模型说"这段原文在这个片段里"，程序回原文核对。

    数字型 claim 问的是"这个数在不在这个字段"，引文型问的是"这句话在不在这份原文"。
    后者才是 knowledge 类结论的主要风险：不是数字编了，而是「手册规定 X」这句话
    手册根本没写——它连数字都可能是对的，只是出处是假的。

    允许引多段：Anthropic 的做法是模型只给位置、程序去文档里切，所以 cited_text
    永远正确；我们让模型自己抄，就必须接受它引两处的情况。按省略号/换行拆开，
    每一段都必须真是原文——引多段可以，编一段不行。
    """
    ref = c.get("ref")
    if not ref:
        stats["unsourced"] += 1
        blocked.append(text or quote[:40])
        return
    m = re.match(r"^\s*(\d{1,3})#(.+?)\s*$", str(ref))
    if not m:
        stats["ref_bad"] += 1
        misquoted.append(f"{text}（出处格式错:{ref}）")
        return
    k, path = int(m.group(1)), m.group(2)
    e = by_no.get(k)
    if e is None:
        stats["cite_oob"] += 1
        blocked.append(f"{text}（证据 {k} 不在本轮可见范围）")
        return
    obj = _load_obj(e)
    if obj is _MISS:
        stats["ref_bad"] += 1
        misquoted.append(f"{text}（证据 {k} 不是结构化返回）")
        return
    got = _get_path(obj, path)
    if got is _MISS:
        if e.get("truncated"):
            stats["cite_cut"] += 1
            blocked.append(f"{text}（证据 {k} 返回体过大已压缩，该字段未保留）")
        else:
            stats["path_miss"] += 1
            misquoted.append(
                f"{text}（证据 {k} 是 {e.get('tool', '?')}，其中没有字段 {path}）")
        return
    if not isinstance(got, str):
        # 引文必须有原文可核。ref 指到一个数值，就没有"原文"这东西可引。
        stats["quote_bad"] += 1
        misquoted.append(f"{text}（ref 指向的不是原文，撑不住一句引文）")
        return
    segs = [s for s in re.split(r"…+|\.{3,}|\n", quote) if s.strip()]
    bad = [s for s in segs if _norm_quote(s) not in _norm_quote(got)]
    if bad:
        stats["quote_bad"] += 1
        misquoted.append(f"{text}（引文在 {k}#{path} 里找不到：{bad[0][:28]}）")
    else:
        stats["quote_ok"] += 1


def _verify_claims(doc, ev, stats, blocked, misquoted):
    """按 claim 逐条校验：一个数字一条，各自带 ref。

    这条路把「引用」从文本解析搬到结构字段，所以三类老毛病结构性消失：
      span      一句三个数字共用一个锚点 —— 想写三个就必须写三条 claim
      写错/没写 混在同一个解析失败里 —— ref 空是没写，ref 指不到是写错
      标灰看不见 只能记在报告里     —— 渲染时把标记跟在数字后面
    """
    # 证据按自带编号建索引。没有 ev_no 的（旧数据、测试构造）不入索引，
    # 引用它会直接判"不在可见范围"——宁可显式失败，也不能静默退回按下标算，
    # 那样两套编号语义并存，哪天 act 漏写编号就没人发现。
    by_no = {}
    for e in ev:
        no = e.get("ev_no")
        if no is not None:
            by_no[int(no)] = e

    # 把数字全塞进 conclusion、claims 给空数组——这条通道刚被发现。
    # 后果不是判错，是"什么都不判"：qty=0，报告上既不红也不绿，和刚撤掉的豁免
    # 属于同一类洞，只是这次是从另一侧开的。结论里写了数字，就必须有一条 claim
    # 去认领它；claims 为空而 conclusion 干净（如 lookup 的"不做根因判断"）不算。
    claims = doc.get("claims") or []
    if not claims:
        bare = re.findall(r"\d+(?:\.\d+)?",
                          _IDENT_RE.sub(" ", str(doc.get("conclusion") or "")))
        if bare:
            stats["bare_conclusion"] = 1
            misquoted.append(
                f"结论里写了数字（{'、'.join(bare[:5])}）但 claims 为空，没有一个能被核验"
            )

    for c in claims:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        unit = str(c.get("unit") or "").strip()
        v = c.get("value")
        quote = str(c.get("quote") or "").strip()
        if v is None and quote:
            # 引文型：没有数字，但有它声称抄来的原文，回原文核对
            _verify_quote(c, text, quote, by_no, stats, blocked, misquoted)
            continue
        if v is None:
            continue          # 纯论述型 claim，既没数字也没引文，无从核对
        stats["qty"] += 1
        if unit == "%":
            stats["pct"] += 1

        ref = c.get("ref")
        if not ref:
            stats["unsourced"] += 1
            blocked.append(text or str(v))
            continue

        m = re.match(r"^\s*(\d{1,3})#(.+?)\s*$", str(ref))
        if not m:
            stats["ref_bad"] += 1
            misquoted.append(f"{text}（出处格式错:{ref}）")
            continue
        k, path = int(m.group(1)), m.group(2)

        # 按证据自己带的编号找，不按下标算。模型抄的是它在 "[证据 N]" 里看到的
        # 那个号，和 ev 列表的第几项不是同一件事——_ctx_window 裁掉前几轮之后，
        # ev[0] 可能正好是证据 7。按下标算就等于要求模型同时记住"看到的是第几条"
        # 和"裁完以后算第几条"，而这正是它唯一会错的地方。
        e = by_no.get(k)
        if e is None:
            stats["cite_oob"] += 1
            blocked.append(f"{text}（证据 {k} 不在本轮可见范围）")
            continue

        obj = _load_obj(e)
        if obj is _MISS:
            # 不该发生：_payload 已保证输出永远是合法 JSON（超限时降级成
            # _payload_overflow 包装对象）。真到了这里说明有别处写坏了证据。
            stats["ref_bad"] += 1
            misquoted.append(f"{text}（证据 {k} 不是结构化返回）")
            continue

        got = _get_path(obj, path)
        if got is _MISS:
            # 证据被压缩装不下时，字段本身可能就没送进来。这时模型写的路径
            # 也许完全正确，只是那条证据里已经没有它 —— 判模型写错是误判。
            # 和"字段真不存在"分开：前者放行标灰，后者判失败。
            if e.get("truncated"):
                stats["cite_cut"] += 1
                blocked.append(f"{text}（证据 {k} 返回体过大已压缩，该字段未保留）")
            else:
                stats["path_miss"] += 1
                # 必须带上编号和工具名。只写"字段不存在:xxx"，读起来就是"模型编了个
                # 不存在的字段"，第一反应会去改 prompt；但这一类的成因可能是
                # 引错了证据，也可能真是字段名错——两种修法完全不同，
                # 文案里必须能分开。
                tool = e.get("tool", "?")
                misquoted.append(
                    f"{text}（证据 {k} 是 {tool}，其中没有字段 {path}）"
                )
            continue

        vmax = c.get("value_max")
        if vmax is not None:
            # ---- 区间 claim：合法性完全取决于 ref 指向什么 ----
            # 这是"引用单位"那条设计在代码里的落点：
            #   ref 指文本字段（原文里就有「上升 10%-25%」）→ 这是一段可引的原文，
            #     两端都能在里面找到，属于合法引用
            #   ref 指数值字段（如 0.15）→ 这个位置只担保一个精确值。拿它担保
            #     一个区间，等于让精确位置为不精确断言背书，判失败
            # 改之前没有 value_max 这个字段，区间只能把下界塞进 value，
            # 上界根本不参与校验——"预计降到 6%~99%" 配 value=6 能全过。
            try:
                lo = float(v)
                hi = float(vmax)
            except (TypeError, ValueError):
                stats["ref_bad"] += 1
                misquoted.append(f"{text}（区间端点不是数字:{v}/{vmax}）")
                continue
            if hi < lo:
                stats["range_bad"] += 1
                misquoted.append(f"{text}（区间上下界写反:{lo}>{hi}）")
                continue
            if not isinstance(got, str):
                stats["range_bad"] += 1
                misquoted.append(
                    f"{text}（ref 指向数值字段 {got}，撑不住区间 {lo}~{hi}）")
                continue
            # 文本字段：两端必须同时出现。只找到一端不算——
            # 那说明另一端是模型自己补的，不是原文里的。
            if _value_match(lo, got, unit) and _value_match(hi, got, unit):
                stats["path_ok"] += 1
                stats["cited_path"] += 1
                stats["cited_ok"] += 1
                stats["range_ok"] += 1
            else:
                stats["range_bad"] += 1
                misquoted.append(f"{text}（区间 {lo}~{hi} 与该字段原文对不上）")
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
    # 取证据的规则收在 _visible_evidence 一处：check 也调它，两边口径必须一致，
    # 否则 check 会放行一条模型读不到的旧证据，verify 再判 cite_oob。
    ev = _visible_evidence(state)

    blocked = []
    # 未溯源 ≠ 伪引用。两个列表装的是两类东西：
    #   blocked    ref 写成 null —— 模型明说"这个数没有直接出处"，标灰放行
    #   misquoted  ref 指到了字段、但那个字段撑不住这个数 —— 判失败
    # 前者可能是算出来的、数出来的、或自己定的目标，程序无从判断，不该判死；
    # 后者是明确声称了出处又对不上，性质不同，混在一起等于判同一个刑。
    # check 抓到的额外问题要并进来：那是"回环后用 ref=null 换通过"这类绕法。
    # 不并进来的话，它就是个写了没人读的字段，而且绕法本身会静默通过。
    misquoted = list(state.get("check_notes") or [])
    stats = _new_stats()

    # 结构化契约：结论必须是 claims JSON。引用是字段，不是文本里的字，
    # 所以不用再去正则里猜"这个数属于哪个锚点"。
    # 这条对**所有**路径生效，不留豁免：Anthropic 的 citations 形状里没有
    # "某条路径不做引用保证"这种通道，真做不到就该明确声明，而不是关掉检查。
    doc = _parse_claims(final)
    # 节点自己排好版的（lookup 的表格）不要用列表渲染覆盖：
    # claims 给校验层，final_text 给人看，两份各有分工。
    preset_text = str(state.get("final_text") or "").strip()
    if doc is not None:
        # 引号修复救回来的要计数：修复是静默的，不记就看不见模型多不守规矩。
        if doc.pop("_repaired", False):
            stats["json_fixed"] = 1
        _verify_claims(doc, ev, stats, blocked, misquoted)
        # 兜底文案形状上是合法结论（claims 空、conclusion 无数字），
        # 光看形状会静默通过；但那段话里没有任何被整理出来的内容，
        # 拿它当"通过"就是把一次基础设施故障记成了成功。
        # 判失败，但原因写明是"半成品"，不和"引用写错"混为一类。
        svc = str(state.get("svc_fail") or "").strip()
        if svc:
            stats["svc_fail"] = 1
            misquoted.append(
                f"整理答案的模型调用失败（{svc}），这一条是半成品，不能当结论用"
            )
        final_text = preset_text or _render_claims(doc)
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


def _data_block(prof, log, ev_prof, ev_log):
    """拼这台设备的档案块，同时把每个数字记成一条 claim。

    为什么要在拼的时候顺手记：文本里的数字和 evidence 里的字段原本毫无绑定，
    拼接代码抄错字段（把 false_positive_rate 当成 rate）不会有任何报错。
    而拼的这一行，正是唯一"知道这个数来自哪个字段"的时刻——过了就没了。

    不再是"程序拼文本、校验层另找一份事实"：文本和 claim 出自同一处取值，
    同一份事实只有一份。
    """
    claims = []

    def add(text, value, unit, ref):
        # 字段缺失时不记 claim：没有值的数字本来就不该出现在结论里，
        # 记一条 value=None 的 claim 只会让校验层多一条无从判断的条目。
        if value is None or value == "" or value == "-":
            return
        claims.append({"text": text, "value": value, "unit": unit, "ref": ref})

    prof_ref = f"{ev_prof}#"
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
    add(f"保养周期 {prof.get('maintenance_cycle_days')} 天",
        prof.get("maintenance_cycle_days"), "天", prof_ref + "maintenance_cycle_days")
    add(f"日志条数 {prof.get('log_count')} 条",
        prof.get("log_count"), "条", prof_ref + "log_count")

    if isinstance(log, dict) and "error" not in log:
        log_ref = f"{ev_log}#"
        lines += [
            "",
            f"最近 7 天（{log.get('time_from', '-')} ~ {log.get('time_to', '-')}）："
            f"共判定 {log.get('total', 0)} 条，过杀 {log.get('false_positive_count', 0)} 条，"
            f"整体误判率 {_pct(log.get('false_positive_rate', 0))}",
            "",
            "| 工位 | 判定 | 过杀 | 误判率 |",
            "|---|---|---|---|",
        ]
        add(f"共判定 {log.get('total')} 条", log.get("total"), "条", log_ref + "total")
        add(f"过杀 {log.get('false_positive_count')} 条",
            log.get("false_positive_count"), "条", log_ref + "false_positive_count")

        rate = log.get("false_positive_rate")
        add(f"整体误判率 {_pct(rate)}",
            (round(float(rate) * 100, 2) if isinstance(rate, (int, float)) else rate),
            "%", log_ref + "false_positive_rate")

        for i, g in enumerate((log.get("groups") or [])[:8]):
            lines.append(
                f"| {g['key']} | {g['count']} | {g['false_positive']} | {_pct(g['rate'])} |"
            )
            add(f"{g['key']} 判定 {g['count']} 条",
                g.get("count"), "条", f"{log_ref}groups[{i}].count")
            add(f"{g['key']} 过杀 {g['false_positive']} 条",
                g.get("false_positive"), "条", f"{log_ref}groups[{i}].false_positive")
            rate = g.get("rate")
            add(f"{g['key']} 误判率 {_pct(rate)}",
                (round(float(rate) * 100, 2) if isinstance(rate, (int, float)) else rate),
                "%", f"{log_ref}groups[{i}].rate")

    return "\n".join(lines), claims


def lookup(state):
    """只读查询：把数据直接给出来，不做根因判断。

    刻意不让模型组织这段文字。之前输入「cx-104 数据」时，模型把「数据」理解成
    「误判分布」并直接给了根因结论——用模板拼能从根上杜绝它替用户补意图。
    """
    ids = (state.get("device_ids") or [])[:3]
    blocks, evidence, names, steps, claims = [], [], [], 0, []

    turn = int(state.get("turn") or 1)
    # 编号沿用 act 那套绝对号（跨轮递增）。lookup 的证据也得有号：
    # 否则同一份 evidence 里并存"有号"和"没号"两种条目，多轮下必然打架，
    # 而且模型在 lookup 结论里看到的数字，事后没有任何编号可以指回去。
    base = len(state.get("evidence") or [])
    for d in ids:
        prof = query_device_profile(d)
        prof_no = base + len(evidence) + 1
        evidence.append({"tool": "query_device_profile", "args": {"device_id": d},
                         "result": prof, "turn": turn, "ev_no": prof_no})
        names.append("query_device_profile")
        steps += 1
        if isinstance(prof, dict) and "error" in prof:
            known = prof.get("known_devices") or []
            blocks.append(f"### {d}\n\n未查到这台设备。当前库里有：{', '.join(known)}")
            continue
        log = query_defect_log(d, group_by="station")
        log_no = base + len(evidence) + 1
        evidence.append({"tool": "query_defect_log",
                         "args": {"device_id": d, "group_by": "station"},
                         "result": log, "turn": turn, "ev_no": log_no})
        names.append("query_defect_log")
        steps += 1
        text, block_claims = _data_block(prof, log, prof_no, log_no)
        blocks.append(text)
        claims.extend(block_claims)

        # 工单也属于"这台设备的数据"，在这里一并给出来。
        # 之前这里只查档案与误判统计，导致用户问「上次那个工单处理了吗」被判成 lookup 后
        # 只能拿到档案和误判率——明明库里 5 条工单，却答不出工单状态。
        # 不做语义判断、不猜用户是不是想问工单：只要查设备数据，就把工单一起列出来。
        # 刻意不渲染 root_cause 全文：lookup 的合同是"不做根因判断"，
        # 把根因描述摆进给用户的文本里，等于变相给了结论。
        wo = query_work_order(device_id=d)
        wo_no = base + len(evidence) + 1
        evidence.append({"tool": "query_work_order",
                         "args": {"device_id": d}, "result": wo, "turn": turn,
                         "ev_no": wo_no})
        names.append("query_work_order")
        steps += 1
        if wo.get("state") == "found":
            lines = "\n".join(
                f"- {o['order_id']} | {o['status']} | 严重度 {o['severity']} | "
                f"{o['assignee_group']} | 建单 {o['created_at']}"
                for o in wo["orders"]
            )
            blocks.append(f"### {d} 历史工单（{wo['count']} 张）\n\n{lines}")
            claims.append({"text": f"历史工单 {wo['count']} 张",
                           "value": wo["count"], "unit": "张",
                           "ref": f"{wo_no}#count"})
        else:
            blocks.append(f"### {d} 历史工单\n\n{wo.get('note', '没有工单记录')}")

    body = "\n\n".join(blocks) if blocks else "没有可用的设备编号。"
    tail = (
        "\n\n---\n以上是设备档案、最近 7 天的误判统计与历史工单，**没有做根因判断**。"
        "需要排查原因的话，说一句现象（哪类缺陷、从什么时候开始、哪个班次更重），我再往下查。"
    )
    # final 走 claims 契约（给校验层），final_text 保留表格（给人看）。
    # 两份出自同一处取值：拼这一行的时候就知道这个数来自哪个字段，
    # 所以"文本里的数字"和"ref"不可能不一致——它们不是各算一遍。
    doc = {
        "conclusion": "按设备档案与最近 7 天的误判统计列出如下，不做根因判断。",
        "claims": claims,
        "advice": [],
    }
    trace.emit("lookup", device_ids=ids, blocks=len(blocks), steps=steps,
               claims=len(claims))
    return {"final": json.dumps(doc, ensure_ascii=False),
            "final_text": body + tail,
            "evidence": evidence, "tool_names": names, "steps": steps}


def _prose_doc(text):
    """程序自己写的声明也走同一个结论结构。

    "服务不可用 / 检索失败 / 手册没收录"这三条是程序的诚实声明，不是模型结论。
    让它们也长成 {"conclusion", "claims": []} 不是为了让校验放行：
    空 claims 本身是有含义的——"这条结论里没有可引用的具体事实"，
    报告上 qty=0 看得见。豁免才是把这件事藏起来。
    """
    return json.dumps({"conclusion": text, "claims": [], "advice": []},
                      ensure_ascii=False)


KNOWLEDGE_PROMPT = """你在回答视觉检测设备的规则/标准/含义类问题。

只能使用用户给出的手册片段回答，且只能输出一个 JSON 对象，不要输出任何 JSON 以外的文字：

{"conclusion": "结论正文，可以分段，段间用 \\n",
 "claims": [{"text": "这条结论里的一个具体事实", "quote": "它对应的原文，逐字抄", "ref": "片段编号"}]}

规则：
1. 结论里每一个来自手册的具体事实（数值、阈值、规则、含义、条件、步骤）都单独写成一条 claim。
   一条只讲一件事：一句话里有三个事实就写三条，不要合并成一条。
2. quote 必须从对应片段里逐字复制，不要改写、不要概括、不要缩写。
   一句话要引用两个地方就写两条 claim，不要用省略号把两段缝成一条引文。
3. ref 只写片段编号（如 "2"），且必须是你收到的片段编号。
   引不出原文的内容不要写进 claims，也不要写进 conclusion。
4. 片段里没有的不要补。确实没有时，conclusion 写"手册中未收录"，claims 给空数组。
5. 不同片段对同一参数给出不同数值时，必须在 conclusion 里指出这是冲突并列出各出处，不要自己挑一个。
6. 不要编造任何数字。"""


def knowledge(state):
    """知识查询：查手册并回答，明确标注出处。

    这里刻意实现「三态」——检索服务不可用 / 没检索到 / 检索到了，
    三种情况三种回答，避免模型把「服务挂了」说成「手册里没有」。
    """
    q = state.get("user_msg", "")
    names, evidence, steps = [], [], 0
    # 同 lookup：证据必须带绝对号。knowledge 的答案里的数字事后要靠它指回去。
    base = len(state.get("evidence") or [])

    try:
        res = search_manual(q, top_k=5)
    except Exception as exc:
        trace.emit("knowledge", status="service_unavailable",
                   error=f"{type(exc).__name__}: {exc}")
        return {"final": _prose_doc(
            f"手册检索服务当前不可用（{type(exc).__name__}: {exc}）。\n\n"
            "注意：这不是「手册里没有」，而是检索本身没跑起来——不要据此推断标准值。"
            "可以稍后重试，或直接查阅 SOP 原文。"
        )}

    names.append("search_manual")
    steps += 1
    search_no = base + len(evidence) + 1
    evidence.append({"tool": "search_manual", "args": {"query": q}, "result": res,
                     "turn": int(state.get("turn") or 1), "ev_no": search_no})

    if isinstance(res, dict) and "error" in res:
        trace.emit("knowledge", status="tool_error", error=str(res["error"])[:120])
        return {"final": _prose_doc(
            f"检索失败：{res['error']}\n\n这是检索问题，不代表手册里没有这条内容。"
        ), "evidence": evidence, "tool_names": names, "steps": steps}

    chunks = (res or {}).get("chunks") or []
    if not chunks:
        trace.emit("knowledge", status="empty", chunks=0)
        return {"final": _prose_doc(
            "手册里没有检索到相关内容。可能是这个说法在文档中不存在，"
            "换个说法再问；也可能这条规则确实没有写进手册。"
        ), "evidence": evidence, "tool_names": names, "steps": steps}

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
                         "result": c, "turn": int(state.get("turn") or 1),
                         "ev_no": base + len(evidence) + 1})
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
        svc_fail = ""
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
        # 整理答案这一步失败（多为中转站排队超时），不是模型不守契约。
        # 兜底也必须长成同一个形状：原来这里输出的是纯文本，verify 只能判
        # json_bad，报告上就成了"结论未按 claims 契约输出"——把基础设施故障
        # 报成模型违规，又一次归因错人。
        # 但也不能只改成 prose doc 就算完：那会让它静默通过（qty=0、claims 空、
        # conclusion 里没数字），而这条结论其实是废的。所以单独打一个标记，
        # 由 verify 判失败并写明"半成品"，和引用问题分开记。
        svc_fail = type(exc).__name__
        answer = _prose_doc(f"（整理答案时调用失败：{svc_fail}）"
                            f"手册片段已取到，但未能整理成结论，请人工核对原文")

    doc = _parse_claims(answer)
    if doc is None:
        # 模型没按契约输出，原样交给 verify 判 json_bad。
        # 这里不补一个空 claims 的壳——替模型把格式修好，下次它还是不会写。
        trace.emit("knowledge", status="prose", chunks=len(chunks),
                   answer_len=len(answer))
        return {"final": answer, "evidence": evidence,
                "tool_names": names, "steps": steps}

    # ref 是片段编号（模型只看得见 [1]..[5]），要换成校验层认的绝对号 + 字段路径。
    # 纯机械映射，不改模型说了什么：它说"片段 3"，程序翻成"证据 7 的 chunks[2].text"。
    # 越界编号原样留着不修——校验层会记 ref_bad；替它改成"正确"的编号就是合伙作弊。
    for c in (doc.get("claims") or []):
        if not isinstance(c, dict):
            continue
        r = str(c.get("ref") or "").strip()
        if r.isdigit():
            i = int(r) - 1
            if 0 <= i < len(chunks):
                c["ref"] = f"{search_no}#chunks[{i}].text"

    trace.emit("knowledge", status="ok", chunks=len(chunks),
               claims=len(doc.get("claims") or []))
    # svc_fail 一路传到 verify：兜底文案长得像正常结论（claims 空、conclusion 无数字），
    # 只靠形状分不出来，必须由节点自己说"这一条没跑成"。
    return {"final": json.dumps(doc, ensure_ascii=False),
            "final_text": _render_claims(doc), "svc_fail": svc_fail,
            "evidence": evidence, "tool_names": names, "steps": steps}


def finalize(state):
    """收尾，并给本轮写一句话摘要。

    摘要用规则拼、不再调一次模型：可复现、不花钱、出问题能定位。
    上下文窗口裁剪掉的轮次，就靠这段文字留在 system prompt 里。
    """
    turn = int(state.get("turn") or 1)
    tools = ",".join(state.get("tool_names") or []) or "无"
    user_head = (state.get("user_msg") or "").replace("\n", " ")[:50]
    final_head = (state.get("final") or "").replace("\n", " ")[:80]
    # 结束原因写进跨轮摘要：下一轮模型能看到"上一轮是被预算掐断的"，
    # 就不会把一份半成品结论当成已经查实的事实继续往下推。
    # lookup / knowledge 不走 plan，自然没有这个值，写成"未跑循环"而不是空——
    # 空字符串和"没跑过"在事后统计里长得一样，又是一份分不出来的事实。
    stop = state.get("stop_reason") or "未跑循环"
    line = (f"\n[{turn}] 用户「{user_head}」→ {state.get('intent') or '?'}；"
            f"工具 {tools}；结束={stop}；结论「{final_head}」")
    return {"final": state.get("final", ""), "session_summary": line}
