"""图节点。

设计原则：L2 方向判断完全交给模型——代码里不出现"如果某工位误判率高就查光源"这类分支。
程序只做三件模型做不好的事：L1 信息补全、工具执行、结论校验。
"""

import ast
import datetime
import json
import operator
import os
import re
import time

from openai import OpenAI

from app import trace
from app.graph.llm import CALL_TIMEOUT, MODEL, get_client
from app.graph.state import STATE_VER, fresh_reset_values
from app.tools import TOOL_IMPLS, TOOL_SCHEMAS
from app.tools.diagnosis import query_defect_log, query_device_profile
from app.tools.manual import check_doc_conflict, search_manual
from app.tools.workorder import query_work_order

# 工具调用预算。可用环境变量 FDE_MAX_STEPS 覆盖，默认 12。
#
# 为什么要能覆盖：`budget_exhausted` 这条分支必须真实触发过一次才能引用。
# 默认 12 步时模型几乎总是自己收工——103 条运行记录里 `stop_reason` 有值的
# 6 条全是 `enough`，`budget_exhausted` 出现 0 次。把它调成 3 跑一次，
# 就能拿到"被预算掐断时结论长什么样、report 怎么标"的真实样本。
#
# 注意它在 import 时求值：改了环境变量要新起进程才生效（本来也该如此）。
MAX_STEPS = int(os.environ.get("FDE_MAX_STEPS") or 12)

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
    {"text": "含数字的原话", "value": 90, "unit": "天", "ref": "query_device_profile#maintenance_cycle_days"},
    {"text": "误判率 58.82%", "value": 58.82, "unit": "%", "ref": "query_defect_log#groups[0].rate"},
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
   - ref 写这个数的出处：**工具名 + "#" + 字段路径**。
     工具名照你实际调用的那个工具写（query_device_profile、query_defect_log、
     query_calibration、search_manual…），一个字母都不要改。
     不要写"第几条"或数字编号——位置会随对话变，工具名不会。
     字段路径照工具返回的 JSON 原样写：顶层 query_device_profile#maintenance_cycle_days、
     数组下标 query_defect_log#groups[0].count、嵌套 query_calibration#records[0].drift_px。
   - 同一个工具你调了不止一次（比如先按工位查、再按缺陷类型查），
     工具返回的最前面会印 `[query_defect_log{group_by=station}]` 这样的标签。
     这时 ref 要**把花括号那一截照抄进去**：
     `query_defect_log{group_by=station}#groups[0].rate`。
     不抄也能过校验，但两条返回的字段路径长得一模一样（都有 groups[0].rate），
     分不出你指的是哪一次——抄上去才是明确指认。
   - 一个数值要挂到数组里的某条记录上（比如"11-10 亮度 85、11-11 降到 80"，
     或者四次标定各自的漂移量），把这条 claim 写成 items 数组：
     {"text": "亮度逐次下调", "ref": "query_light_usage#brightness_history",
      "items": [{"key": "2025-03-12", "field": "value", "value": 85},
                {"key": "2026-09-15", "field": "value", "value": 80}]}
     key 写那条记录里的标识（日期、批次、工位名都行，写日期写日期部分就够），
     field 是那条记录里装着这个数的字段名，不确定可以省略。
     不要写成一条 value 然后 ref 指整个数组——那样判据只能确认数字出现过，
     确认不了它属于哪条记录。
   - 自己算出来的值（比值 / 差值 / 加总）ref 写 null，但**必须给算式**，否则这条
     会被记成"没有出处"。算式写在 text 的括号里，或单独放 expr 字段：
     {"text": "74%（=20/27）", "value": 74, "unit": "%", "ref": null, "derived": true, "expr": "20/27"}
     算式要能直接算出 value——百分比会自动按 % 换算，20/27 这样写就够了。
     算错了比不写更糟：不写只标灰，写错判引用失败。
   - 自己定的验收目标、假设值，ref 也写 null。
   - 没有数字的论述写进 conclusion 或 advice，不要塞进 claims。
   - ref 写错比写 null 更糟：拿不准就不写字段路径，写 null。
   - text / advice / conclusion 里要强调某个词，用中文引号「」，不许用英文双引号。
     外层 JSON 本身就是双引号，你中间插一个裸双引号，字符串当场截断，
     整份结论解析失败——你写对的内容一个也用不上，还全被判成"引用无法核验"。
     实测反例：advice 里写「无法严格量化"夜班vs白班"差异」，
     JSON 在第 2339 字符处断掉，14 条正确的引用全部作废。

状态声明有边界：你说的是"根因判断 + 建议处置"，不是现场结果。
   - 不许写「故障已排除」「问题已解决」「已修复」「已恢复正常」「维修完成」「完工」
     这类终态表述。设备在现场，你没有任何观测手段确认它真的好了；
     写下去等于替现场宣布一个你没看到的结果。
   - 也不许写「工单已创建并派工完成」：create_work_order 建的是**待人工确认的草稿**，
     回读成功只证明"记录进去了"，不证明"现场收到并处理了"。
   - 想表达推进程度，写"建议按以下步骤处置，处置结果需现场回报确认"。

回答用中文。conclusion 说根因，claims 放全部带数字的证据，advice 说处置。
"""


def intake(state):
    msg = state.get("user_msg", "")
    m = DEVICE_RE.search(msg)
    device_id = m.group(0).upper() if m else ""
    turn = int(state.get("turn") or 0) + 1
    # 旧会话接着用新代码：记一条迁移。
    # 不记的话，"这条会话为什么字段对不上"只能靠翻 commit 猜是哪一版。
    # 全新会话 prev_ver=0，那是从零开始，不是迁移，不产生这条事件——
    # 否则每次首轮都刷一条噪音，真迁移时反而没人看。
    prev_ver = int(state.get("state_ver") or 0)
    if 0 < prev_ver < STATE_VER:
        trace.emit("migrate", from_ver=prev_ver, to_ver=STATE_VER, turn=turn)
    trace.emit("intake", device_id=device_id, matched=bool(m), msg_len=len(msg), turn=turn)
    # 本轮字段的清单在 state.RESET_EACH_TURN，这里只补四个每轮现算的：
    #   device_id —— 正则结果，作为 understand 的提示
    #   messages  —— 本轮这条 user 消息（其余历史靠 reducer 累积）
    #   turn      —— 轮次 +1
    #   state_ver —— 当前 schema 版本（跨轮保留，不重置）
    # 加新字段时去 RESET_EACH_TURN 登记，不要再往这个 return 里抄一遍。
    return {
        **fresh_reset_values(),
        "device_id": device_id,
        "messages": [{"role": "user", "content": msg}],
        "turn": turn,
        "state_ver": STATE_VER,
    }


UNDERSTAND_PROMPT = """你在给一台视觉检测设备诊断系统做第一层意图判断。只做分类，不做诊断，不要调用任何工具。

判断用户这句话属于哪一类：

- diagnose：用户描述了异常现象，想找原因。
  例：误判率高、良率掉了、夜班更差、从某天开始变多、是不是光源导致的
  **变化型也算**：凡是"换了什么之后变多 / 最近一个月越来越多 / 昨天开始一下子变多"，
  有变化描述又想知道为什么，一律 diagnose。换料、换供应商、换光源都属于这一类。
- lookup：用户想看**这台设备**的数据、配置、参数、记录本身，没有要找原因的意思。
  例：给我看下数据、cx-104 的保养周期设的是多少、这台上次标定是什么时候、最近记录
  边界：问的是"某台设备实际怎么配的、上次/现在是多少"，即使问的是参数名，也归 lookup。
  参数名本身是知识，但这台设了多少是数据。
  **反例**：用户在说"变多了 / 下降了 / 换了 X 之后"并要原因时，是 diagnose。
  「误判变多」不是「想看误判数据」：前者要原因，后者要明细。
- knowledge：用户在问规则、标准、含义、操作步骤。这类问题**不依赖具体设备**也能回答。
  例：E-101 是什么意思、保养周期规程要求多久、亮度标准值是多少、怎么清洁镜头
- action：用户要求执行一个动作（建单、派工、登记、通知），不是在问原因。
  例：帮我建个工单、派给设备科、登记一下、再建一次刚才那个单
- refuse：用户**明确要求你放弃取证、凭印象给一个数**，或要求你给出没有依据的估计。
  例：不用查了，你凭经验估一下、你就说个大概的数、别管数据了直接给我个值
  边界：这是唯一一类"用户要的东西和系统的保证互斥"的请求——结论必须有证据支撑，
  而"凭经验编"恰好不要证据。遇到就判 refuse，不要退成 lookup 去给一堆数据，
  那等于用无关数据冒充答案。用户只是问"大概多少"但仍接受你查一下，仍属于 diagnose/lookup。
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
  "intent": "diagnose|lookup|knowledge|action|refuse|clarify",
  "confidence": 0.85,
  "device_ids": ["CX-104"],
  "alarm_codes": ["E-101"],
  "param_names": ["光源亮度设定"],
  "phenomenon": "用户描述的异常现象（概括原话），没有就填空字符串",
  "missing": ["还缺什么信息才能继续"],
  "reason": "一句话说明凭什么这么判"
}"""

# 模型判为诊断/查询但输出字段不自洽时的降级说明
_INTENT_ENUM = {"diagnose", "lookup", "knowledge", "action", "refuse", "clarify"}

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

# ---- 分支的"猜错代价"：决定低置信时是问还是做 ----
#
# 依据是 clarify-or-commit（arXiv 2601.06407）：每一轮真正要决策的不是"我确不确定"，
# 而是"问一句的收益"和"问一句的成本 / 猜错的损失"哪个大。
# 原文：every question carries both the potential to reduce uncertainty
#      and **the cost of additional user effort**。
#
# 分值含义：2 = 猜错要跑很多步且答案不像"答非所问"，用户很难中途发现；
#          3 = 有外部副作用（建单），错了要回滚；
#          0 = 一步就能给出，答偏了用户一眼看出来，一句话就能纠正
#              （REVOIR 的 cheap correction：此时先做比先问更划算）。
#
# 这张表替代了"低置信一律追问"：lookup/knowledge 现在低了也照做，
# 但会把假设写在结论最前面（见 finalize）。
BRANCH_COST = {"action": 3, "diagnose": 2, "lookup": 0, "knowledge": 0, "refuse": 0}

# ---- 确定性前置：只在"罕见组合"上下手 ----
#
# 为什么只留这三条：在 27 条夹具上逐条测过精度——
#   guess（不用查/凭经验）           1 条, 100%
#   act_verb（建/派 + 工单）          2 条, 100%
#   change+dev（变化词 + 设备号）      4 条, 100%
#   why+dev                          86%（W3 里"别管什么原因"误伤）
#   look_only                        50%（H2 里"标定记录"误伤）
#   param_only                       33%（H5/W1 里"阈值/亮度"误伤）
#
# 后三条依赖的是**常见词**（记录、阈值、原因），真实用户表达更丰富，误伤只会更多；
# 前三条依赖的是**罕见组合**，不容易撞上。所以弱规则一律不进前置——
# 它们漏掉的情况会落到"追问"，而追问是安全路径；反过来，硬塞进去的弱规则
# 会把本来正确的判断改成错的，且这种错在报表上看起来像"模型退化了"。
_RULE_GUESS = re.compile(r"(不用查|凭经验|估一下|随便给|别管数据)")
# 注意"建"和"工单"必须是动词-宾语关系，不能用「句子里有工单」——
# I9「上次那个工单处理了吗」有"工单"但那是名词，判成 action 就错了。
_RULE_ACT = re.compile(r"(建|派|登记)[^。；！？]{0,6}(单|工单|派工)")
_RULE_CHANGE = re.compile(r"(变多|变少|飙升|越来越高|越来越低|突然|换了|开始变|上去了|掉了)")


def route_by_rule(msg):
    """规则能定就定，返回 (intent, 规则名)；定不了返回 (None, "")。

    不追求覆盖率，追求"出手必对"。命中之后仍然会调模型（要它的 phenomenon /
    param_names），但 intent 不听模型的——这样并发抖动只影响抽取，不影响路由。
    """
    if _RULE_GUESS.search(msg):
        return "refuse", "guess"
    if _RULE_ACT.search(msg):
        return "action", "act_verb"
    if DEVICE_RE.search(msg) and _RULE_CHANGE.search(msg):
        return "diagnose", "change+dev"
    return None, ""


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
    #
    # 先看规则能不能定。规则定得了就定，不再听模型的 intent——
    # 这是这一层最重要的改动：**路由不再由概率模型的输出直接决定**。
    # 模型照旧要调（需要它的 phenomenon / param_names），但它的 intent 只作参考。
    rule_intent, rule_name = route_by_rule(msg)
    if rule_intent:
        intent = rule_intent
        route_by = f"rule:{rule_name}"
    else:
        intent = str(data.get("intent") or "").strip().lower()
        if intent not in _INTENT_ENUM:
            intent = "clarify"
        route_by = "model"

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
    # ---- 不确定时：按"猜错代价"决定问还是做，而不是一律追问 ----
    #
    # 规则命中的不进这道闸：规则是确定性的，模型给的 confidence 对它是噪音。
    # 原来这里只有一个判据（低置信 → 追问），结果是两个相反的错：
    #   A5 那种 spec 其实很完整、只是模型没判对的情况，被一句追问糊过去，
    #      评测上再也看不见"它判错了"（假绿）；
    #   lookup 那种一步就能给答案、答偏了用户一眼看穿的情况，白白多问一轮。
    # 现在按 BRANCH_COST 分开：贵的问，便宜的先做并把假设写在结论最前面。
    if intent != "clarify" and not route_by.startswith("rule:"):
        cost = BRANCH_COST.get(intent, 2)
        if conf < CONF_ASK and cost >= 2:
            intent_note = "你这句话两种理解都通，先确认一句再往下走"
            intent = "clarify"
        elif conf < CONF_ASK:
            # 便宜分支：不问，直接做，但把假设露出来让用户能纠正
            # （REVOIR：能被便宜纠正时，先问反而更费）
            intent_note = f"我按「{intent}」理解（置信 {conf:.2f}），若不对直接纠正"
        elif conf < CONF_OK:
            intent_note = f"意图置信度 {conf:.2f}，按「{intent}」理解，若不对请直接纠正"

    # 模型自己判的那个 intent 提成变量：trace 和 state 都要用，
    # 两处各写一遍表达式迟早漂移。规则覆盖它时，两个值的分歧本身就是
    # 最有价值的样本（规则对、模型错，或反过来）。
    model_intent = str(data.get("intent") or "").strip().lower()[:20]

    trace.emit(
        "understand",
        intent=intent,
        confidence=round(conf, 3),
        device_ids=ids,
        phenomenon=phen[:120],
        missing=list(data.get("missing") or []),
        reason=reason[:160],
        note=intent_note[:120],
        # 判定来源：rule:<名字> / model。用来算每条规则的真实误伤率——
        # 规则错和模型错的归因方式完全不同，混在一起就什么都看不出来。
        route_by=route_by,
        model_intent=model_intent,
        # 降级要留痕。判据只看到 intent=lookup 会把它记成"模型判错"，
        # 而真相应是"这一次根本没判成"——两者的修法完全不同。
        fallback_reason=fallback_reason[:120],
    )

    return {
        "intent": intent,
        "intent_conf": conf,
        "intent_note": intent_note,
        # 不再回写顶层 device_id。它原来写的是 ids[0]，是 device_ids 的副本——
        # 正是 state.py 开头那句"派生不存"要禁的东西。下游也确实没人读它：
        # _owned_device_ids 早就改成只读 device_ids 了，唯一的读者是 understand
        # 自己的 hint，读的是 intake 用正则抽的那份（在本次写之前）。
        # 留着只会让"这台设备是哪台"有两个来源，改一个忘一个。
        "device_ids": ids,
        # 不写顶层 phenomenon：同一句话在 slots 里已经有一份，存两处就会分叉。
        # ask_user 改成直接读 slots["phenomenon"]，读到的就是这一轮写进去的那个值。
        "missing": list(data.get("missing") or []),
        "param_names": [str(p).strip() for p in (data.get("param_names") or []) if str(p).strip()],
        "intent_reason": reason,
        # 写进 state 而不是只发 trace：run_eval 传 trace_run=False，评测链不落 runs/，
        # 只在 trace 里记等于报告里根本没有这个字段。规则误伤率要能被算出来，
        # 它必须跟着每条结果一起进报告。
        "route_by": route_by,
        # 和 route_by 成对：route_by 说"谁判的"，model_intent 说"模型本来判什么"。
        # 缺任一个，规则误伤率都算不出来（例如只知 rule:act_verb 生效，
        # 不知道模型原本想判 diagnose 还是 lookup）。
        "model_intent": model_intent,
        # 必须进 state 而不是只发 trace：run_eval 传 trace_run=False，
        # 评测链不落 runs/，只写 trace 等于报告里没有这个字段，
        # 也就永远算不出"这批里有多少条是降级跑的"。
        "fallback_reason": fallback_reason,
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
    # 拒答是一等公民，不是 lookup 的兜底：这类请求要的东西和"结论必须有证据"
    # 互斥，丢进 lookup 只会拿不相关的数据冒充回答。
    if intent == "refuse":
        return "refuse"
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
    # 现象只读 slots：understand 每轮都会把本轮值写进 slots，顶层不再保留副本。
    # 原来这里读 state["phenomenon"]，等于同一句话有两个存放位置，写漏一处两边就分叉。
    phen = str(slots.get("phenomenon") or "").strip()
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
    #
    # final 也走同一套结论结构（claims 为空是诚实的：追问里没有可引用的事实），
    # 但**必须同时给 final_text**：前端读的是 `d.final_text || d.final`，
    # 只给结构化 final 会让追问这一屏直接显示一段 JSON。
    return {"final": _prose_doc(final), "final_text": final, "last_question": final}


def refuse(state):
    """用户要求凭经验编数时，直接拒答，并且不调任何工具。

    为什么单独一个节点、而不是让它落进 lookup：
    这类请求要的东西和系统的保证是互斥的——结论必须有证据支撑，而"凭经验估"
    恰好不要证据。落进 lookup 会拿档案表去冒充答案：表面上给了内容，
    实际上用户的问题一个字没答，而且那份档案看起来还挺"专业"，最容易被当成完成。

    文案由程序拼、不交给模型：模型被要求"给个准数"时最容易顺着写一个数，
    这正是 Anthropic《Refusals and Fallback》里 `stop_reason: "refusal"`
    作为独立终态的理由——拒绝是一个状态，不是一段可以自由发挥的话。

    刻意不写设备号、不写任何数字：结论结构是空 claims，
    一旦正文出现数字就会触发 bare_conclusion（没有 claim 认领的数字 = 编的）。
    """
    msg = (
        "无法凭经验给出这个数。这台设备的误判率只能从误判日志里读出来，"
        "不能凭印象估一个值——估出来的数没有证据支撑，写进报告就等于编了一个事实。\n\n"
        "要的话我可以按工位和时间把实测的误判数据列出来，你看哪个维度异常。"
    )
    trace.emit("refuse", intent="refuse", reason=state.get("intent_reason", ""))
    return {"final": _prose_doc(msg), "final_text": msg}


# 模型本轮能看到几轮。这一个数同时决定两件必须严格相等的事：
#   1. _context_messages 送几轮 messages   —— 模型据此决定"我还能引哪些返回"
#   2. verify 允许哪几轮的 evidence 参与溯源 —— 校验层据此筛可匹配的工具返回
# 两处一旦不一致，引用就整体落空：模型引的工具，校验层根本没拿它的返回参与比对。
# 表现是「引用率突然归零」或「大面积 cite_oob」，看不出是配置问题，
# 第一反应会去怀疑模型——所以只能有一个常量，两处都从 _ctx_window 派生。
#
# 注意这条耦合的**形状**在档 C 之后变了、但**强度**没变：
# 编号时代错位表现为"k 整体差几"（还算得出来），工具名时代表现为
# "这个工具本轮不可见"（直接判 cite_oob）。后者更难反查，所以更需要同源。
CTX_VISIBLE_TURNS = 2
MAX_CTX_TURNS = CTX_VISIBLE_TURNS      # 兼容旧名：scripts/check_ctx.py 和文档在用


def _ctx_window(cur_turn):
    """本轮可见的轮号闭区间 (最早, 最晚)。上下文裁剪与证据过滤的唯一真相源。"""
    lo = max(1, int(cur_turn) - CTX_VISIBLE_TURNS + 1)
    return lo, int(cur_turn)


def _visible_evidence(state):
    """参与溯源的证据：只取本轮可见轮次内的，和 _context_messages 同源。

    为什么 check 和 verify 必须都调它：两处各取一份的后果在多轮里很难看——
    check（回环前）拿全量证据，能查到一条模型早就读不到的工具返回，于是放行；
    verify（终局）只拿窗口内的，同一处引用判 cite_oob，直接 FAIL。
    表现是**回环白跑一轮仍然失败**，而报出来的话像"模型引错了出处"。

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
        return _fold_old_tool_results(msgs)
    cut = starts[-CTX_VISIBLE_TURNS]
    head = msgs[:1] if cut > 0 else []      # 第一句原始诉求留着，别丢掉上下文起点
    return _fold_old_tool_results(head + msgs[cut:])


# 同一轮内保留几条完整工具结果。
#
# 最初取 3，理由是"诊断链是 档案 → 统计 → 工单 三联"。实测推翻了它：
# 真实一轮 12 步诊断里，模型在写结论时仍需要第 4 步那次 group_by=hour 的明细，
# 而 keep=3 正好把它折掉，于是结论里出现「本轮未取到按小时的夜班/白班明细，
# 故夜班更重这一点未作为定因依据」——模型如实承认缺了它，但它已经没法再补，
# 因为它不知道"还有一条被折的"。
#
# 根因是折叠按**位置**算"深"，而诊断 agent 的调用顺序不按重要性排：
# 第 4 步的维度统计可能就是定因的关键，第 12 步只是复核。
# 同参数覆盖（_fold_old_tool_results 的第 ① 条）解决了一部分（重复调用的旧值），
# 解决不了"每个维度只调一次"的情况——那才是常见形状。
#
# 6 是实测折中：12 步里保住前 6 条不同参数的调用，超出部分才折。
# 真正长链（20+ 步）仍会收紧；此时模型多半已经看到过各维度，折的代价变小。
KEEP_RAW_TOOL_RESULTS = 6
# 折叠时保住开头的字符数。_payload 把 "[工具名{参数指纹}]" 印在头部，
# 保住它，模型就仍然知道"这条我调过、参数是什么"，只是看不到明细。
FOLD_HEAD_CHARS = 180
# 短于这个长度的不折叠：折叠标记本身就有几十字，硬折反而更长。
FOLD_MIN_BODY = 400

# _payload 把 "[工具名{参数指纹}] " 印在每条工具结果的开头，这里按它分身份。
_TOOL_HEAD_PAT = re.compile(r"^\[([^\{]+)\{([^\}]*)\}\] ")


def _tool_identity(msg):
    """(工具名, 参数指纹)，用来判断"这两条是不是同一个查询"。

    解析头部拿到；拿不到时退回消息自带的身份——**绝不能退化成 ("", "")**。
    退成空串的后果是灾难性的：所有"没有头部"的工具结果会被认成同一个查询，
    于是除最后一条外全被标成"已被后一次调用取代"，模型一下子丢掉整轮证据。
    这不是假想：任何不走 _payload 格式化、或不带 [工具名{指纹}] 头的消息
    都会踩到（历史数据、将来某个新工具、手工构造的测试夹具）。
    """
    m = _TOOL_HEAD_PAT.match(str(msg.get("content") or ""))
    if m:
        return m.group(1), m.group(2)
    return (str(msg.get("name") or ""),
            str(msg.get("tool_call_id") or id(msg)))


def _fold_old_tool_results(msgs, keep=KEEP_RAW_TOOL_RESULTS):
    """本轮内较早的工具结果折叠，规则有两条。

    **① 同参数重复调用：只留最后一次完整。**
    这一条是实测逼出来的，不是设计时想到的。真实跑一轮 12 步诊断，模型把
    `query_defect_log(group_by=hour)` 调了 3 次（第 3、8 次）；按"第几条"折叠后
    它的结论里出现了「本次按小时维度的明细数字未能读出，你提到的夜班更重尚无
    量化证据」——模型自己知道还缺这块，又调了一遍，还是拿不到。
    按位置折叠的隐含假设是"越早越不重要"，可它重复调用恰恰说明"越早的还不够"。
    改用参数覆盖后，同参数的三条只剩最后一条完整，另外两条明写"已被第 N 条取代"，
    模型立刻知道该看哪一条。

    **② 其余按位置折叠，保留最近 keep 条完整。**
    依据是 Anthropic 那句「once a tool has been called deep in the message
    history, why would the agent need to see the raw result again?」——判据是深。

    三条边界（都是原文的限制句划出来的）：

    - 只处理**最后一轮之内**的。跨轮整轮裁剪 _context_messages 已经做完，
      这里再动会削掉"上一轮结论所依据的证据"——原文警告的
      "importance only becomes apparent later" 就是这个。
    - 折叠不是清空：头部 "[工具名{指纹}]" 留着，标记里写明原文多长、怎么拿明细。
      看不见的字段引用不出来，这是折叠该付的代价。
    - 短于 FOLD_MIN_BODY 的按位置折叠跳过（折了反而更长）；但**被后一次
      调用取代的条目不受此限**——它没有"值不值得折"的问题，它本来就多余。

    **不改 state**：这是投影，不是真相。消息是 dict，直接改 content 会改到
    state 里同一批对象（list() 只浅拷贝列表）。所以每条都新建 dict。
    evidence 明细一字不动，verify 溯源、报告、traces 全读 evidence。
    """
    last_user = None
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            last_user = i
    if last_user is None:
        return msgs

    tool_idx = [i for i in range(last_user + 1, len(msgs))
                if msgs[i].get("role") == "tool"]
    if not tool_idx:
        return msgs

    out = list(msgs)

    # ① 同参数覆盖
    latest = {}
    for i in tool_idx:
        latest[_tool_identity(out[i])] = i
    superseded = set()
    for pos, i in enumerate(tool_idx, start=1):
        key = _tool_identity(out[i])
        newer = latest.get(key)
        if newer is None or newer == i:
            continue
        newer_pos = tool_idx.index(newer) + 1
        out[i] = {**out[i], "content": (
            f"[{key[0]}{{{key[1]}}}] "
            f"（本轮第 {pos} 条，与第 {newer_pos} 条参数相同，已被后一次调用取代；"
            f"要看数据请读第 {newer_pos} 条）"
        )}
        superseded.add(i)

    # ② 其余按位置折叠
    rest = [i for i in tool_idx if i not in superseded]
    old = rest[:-keep] if keep else rest
    for pos, i in enumerate(old, start=1):
        body = str(out[i].get("content") or "")
        if len(body) <= FOLD_MIN_BODY:
            continue
        note = (f"\n…[已折叠：本轮第 {pos} 条工具结果，原文 {len(body)} 字。"
                f"上面是它的头部（工具名、参数指纹和返回的开头）。"
                f"需要明细就重新调用同一工具；引用只能用头部里看得到的字段。]")
        out[i] = {**out[i], "content": body[:FOLD_HEAD_CHARS] + note}
    return out


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
    left = max(0, MAX_STEPS - used)
    notes = [
        f"[工具预算] 已用 {used}/{MAX_STEPS} 次。"
        "判断标准不是「还剩几次」，而是「够不够」：能给出根因和处置就停，不够才继续。"
        "如果用户本轮已明确要求某个动作（例如建单），在根因已明确时优先完成该动作。"
    ]
    # 快到头了：这时候"还能再查一次"是有诱惑力的，代价却是可能一次结论都交不出来。
    # A3 实测就是这么烧满 12 步、final 为空、关键词 0 命中——查得越多，收得越晚，
    # 结论反而一个字都没有。所以最后两次不该被当成"还能再查"的额度，而是"收口"的额度。
    #
    # 为什么不直接把 MAX_STEPS 调大：那只是把同一个失败点往后挪几分钟。
    # 预算的意义是"到点必须交卷"，不是"多给几次机会"。
    if left <= 2:
        notes.append(
            f"[收口提醒] 只剩 {left} 次工具调用了。"
            "现在优先把已经查到的东西写成结论：根因给到能给的粒度，"
            "查不到的那部分明确列出来（例如「X 在文档里没有记载」），"
            "不要在最后这一次调用上再开一个新的取证方向。"
            "交一个被标注了缺口的半成品，比交白卷好。"
        )
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


def _current_final(state):
    """本轮结论文本的唯一取法。check 与 verify 共用。

    plan 节点只写 messages、从不写 final；verify 原来自己兜了一层
    "final 空了就从最后一条 assistant 消息取"，而 check 没有这层兜底。
    后果是 check 读到空串 → _parse_claims("") 返回 None → 走"没按契约输出"
    的 pass 分支 → **整个回环在诊断路径上从未执行过**（check_rounds 恒 0）。
    单测测不出：它们直接把构造好的 state["final"] 喂给 check，
    绕过了"final 从哪来"这一环。

    修法不是给 check 再抄一份兜底——那是把同一件事写两遍，下次改一处忘一处。
    收成一个函数，两边看到的是同一份结论，这类差异结构性消失。
    """
    text = final_to_text(state.get("final")).strip()
    if text:
        return text
    msgs = state.get("messages") or []
    if msgs and msgs[-1].get("role") == "assistant":
        return (msgs[-1].get("content") or "").strip()
    return ""


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
    doc = _parse_claims(_current_final(state))
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


# 证据标签："[query_calibration] {...}" 或 "[query_defect_log{group_by=defect}] {...}"。
# _payload 写到最前面，_load_obj 负责剥掉。
# 用文本前缀而不是往 JSON 里加 "_ev" 字段，是因为工具返回可能是 list 或标量，
# 加字段要么改结构（模型引用的路径全变）、要么加不进去；前缀对所有类型都成立，
# 且完全不影响 JSON 内部的字段路径。
#
# 这里必须是工具名，不能是编号：编号是位置、会随上下文裁剪漂移，工具名是身份。
# `{参数指纹}` 是第二层身份：同一工具一轮里调两次（换 group_by 再查一遍），
# 光有工具名分不出是哪一次。指纹写的是"这次查的是什么"，不是"这是第几次"——
# 序号会漂，参数不会：模型记得住"按缺陷分组的那次"，记不住"第二次调用"。
_TOOL_TAG = re.compile(r"^\[([a-z][a-z0-9_]{2,40})(\{[^{}]*\})?\] ")

# 引用格式：工具名#字段路径，可选带参数指纹 → 工具名{device_id=CX-104}#字段路径。
# 工具名是身份，不随轮次/裁剪漂移；指纹再把它精确到"哪一次调用"。
# 开头必须是字母且有 3 个字符以上：这样 "1#path" 这种编号写法会直接判格式错，
# 而不是被当成"工具名是 1"再滑到别的分支里。
_REF_PAT = re.compile(r"^\s*([a-z][a-z0-9_]{2,40})(\{[^{}]*\})?#(.+?)\s*$")


def _call_sig(args):
    """这次调用的参数指纹：`device_id=CX-104,group_by=defect`。

    为什么要它：工具名能定"哪个工具"，定不到"哪一次调用"。同一工具换个
    group_by 再查一遍，两次返回都含 `groups[0].rate`，路径完全一样——
    模型写 `query_defect_log#groups[0].rate` 时，到底是按工位的还是按缺陷的？

    指纹取自**参数**而不是序号，是因为参数是语义（模型记得住"按缺陷分组的
    那次"），序号是位置（跨轮必漂）。空值和 None 跳过，键排序，保证同一组
    参数永远得到同一个字符串——校验端要靠它做等值比较。
    """
    if not isinstance(args, dict):
        return ""
    parts = []
    for k in sorted(args):
        v = args[k]
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    sig = ",".join(parts)
    # 超长指纹会挤掉 payload 预算，又没人真能抄下来。截断并留省略号，
    # 两边（payload 与校验）用同一条规则，所以仍能等值比较。
    return sig if len(sig) <= 80 else sig[:79] + "…"


def _split_sig(sig):
    """把 `device_id=CX-102,group_by=station,batch` 拆成键值对。

    不能直接按逗号 split：group_by 从 2026-11-19 起支持多维（"station,batch"），
    值内的逗号和参数分隔符是同一个字符。规则是——**没有 "=" 的片段并回上一个
    键的值**。于是 `group_by=station,batch` 得到 {"group_by": "station+batch"}，
    而 `a=1,b=2` 仍是两个键。
    """
    pairs, key = {}, None
    for tok in re.split(r"[,;]", str(sig or "")):
        tok = tok.strip()
        if not tok:
            continue
        if "=" in tok:
            key, val = tok.split("=", 1)
            pairs[key.strip()] = val.strip()
        elif key:
            pairs[key] = pairs[key] + "+" + tok
    return pairs


def _norm_sig_val(v):
    """逗号与加号视为等价：模型可能把多维值写成 station,batch 或 station+batch。"""
    return str(v).replace(",", "+").replace(" ", "")


def _sig_matches(model_sig, cand_sig):
    """模型写的指纹是否被候选覆盖（子集匹配，值归一）。

    用子集而不是全等：模型常只抄最有区分力的那个参数（如只写 group_by），
    全等比会一律不匹配、退化成"同工具全候选"，指纹等于白写。
    """
    m, c = _split_sig(model_sig), _split_sig(cand_sig)
    if not m:
        return False
    for k, v in m.items():
        if k not in c or _norm_sig_val(c[k]) != _norm_sig_val(v):
            return False
    return True


def _payload(res, limit=1600, tool=None, sig=""):
    """统一出口：模型看到的文本 与 evidence 存的，必须是同一份。

    返回 (text, truncated)。text 永远是**合法 JSON**——这是硬约束，因为
    verify 要用 _load_obj 解析它取字段；一旦不是 JSON，所有引用都会判
    ref_bad，报出来是"模型引用写错"，实际是证据坏了，归因完全反了
    （实测：一条 1606 字的 defect_log 返回就能触发，正确引用全被判失败）。
    —— 除了最前面那截 "[工具名] " 标签，它由 _load_obj 剥掉，见 _TOOL_TAG。

    打印的是工具名而不是编号，这是档 C 的落点。之前印 "[证据 N]" 时，
    模型在单轮里照抄，跨轮却会自己重建一套（第 1 轮查过的设备档案，
    它记成"证据 1"，而真实编号是全局累计的）——同一份代码跑两次，
    一次 14/14、一次 0/15，错的全是编号。编号是位置，会随裁剪漂移；
    工具名是身份，第几轮调它都叫同一个名字。

    所以这里不再给它任何编号可抄：模型看不见编号，就没得重建。
    引用格式随之变成 "工具名#字段路径"，见 _REF_PAT。

    逐档收紧结构；三档都压不住时不再切字符串，改为降级成一个仍可解析的
    包装对象，把能留的内容放 preview 里。模型看到 preview 仍可引用其中的
    数字（_value_match 支持字符串内抽数），校验层也能据此判是"证据截断"
    而不是"模型写错"。
    """
    head = f"[{tool}{{{sig}}}] " if (tool and sig) else (f"[{tool}] " if tool else "")
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

    也不再读顶层 device_id：understand 返回的就是
    `{"device_id": ids[0] if ids else ""}`，它不是独立来源，是 device_ids[0] 的副本。
    读它是同一份事实的第二条通道，两条通道一旦不同步，拦的是哪一条就看运气。
    """
    return {str(x).strip().upper() for x in (state.get("device_ids") or []) if str(x).strip()}


# 会对外产生副作用的工具。目前只有建单一个：它写 QMS，事后撤单要比事前拦一次贵得多。
# 以后加"派工""改状态"这类工具，必须同时加进这个集合，否则闸形同不存在。
WRITE_TOOLS = {"create_work_order"}

# 写意图关键词。**这是启发式，不是分类器**：它会漏（用户说"这得处理一下"）、
# 也会误（用户说"上次那张单后来怎样了"其实是查）。
# 两个方向的代价不对称才这么选：
#   漏 → 模型被拒一次，hint 让它回头问用户"要我建单吗"，代价是多一轮对话；
#   误 → 用户只是提问却被建了单，代价是一张脏工单加一次撤单。
# 所以宁可收紧。只在"动词+单据"连写的形态上命中，单说"工单"不算——
# "查一下工单"里有"工单"，但那是在读。
WRITE_INTENT_KW = (
    "建单", "开单", "开工单", "建工单", "创建工单", "提交工单", "新建工单",
    "报修", "上报", "登记",
)


def _current_question(state):
    """本轮用户原话，用于判断写意图。

    倒序找最后一条 role=user。**拿不到就返回空串，也就是"没有写意图"**——
    这是有意的 fail-closed：不知道用户说过什么的时候，不允许执行写操作。
    反过来的 fail-open 会把"上下文缺失"变成"可以建单"，那正是这个闸要防的事。
    """
    for m in reversed(list(state.get("messages") or [])):
        if isinstance(m, dict):
            role, content = m.get("role"), m.get("content")
        else:
            role, content = getattr(m, "role", None), getattr(m, "content", "")
        if role == "user":
            return str(content or "")
    return ""


def _has_write_intent(state):
    """用户是否已经授权这次写操作。两个来源取 OR。

    1. 用户原话里出现"建单"这类**连写**形态 —— 纯词表，不依赖模型。
    2. understand 判他这一轮是 action。

    为什么必须有第 2 条：第 1 条只认连写。"再帮我给 CX-101 建一次刚才那个换光源的
    工单"是动宾分离，词表一个都没命中；而 understand 的 prompt 里 action 的例子
    原句就写着"再建一次刚才那个单"，它判对了（intent=action, confidence 0.72）。
    闸门却不看这个判断，自己用词表重算一遍返回 False。

    2026-11-19 W1 实测后果：第 2 轮两次 create_work_order 都被这条闸拦成
    "用户没有要求建单"，evidence 里只有第 1 轮那条 deduped=false ——
    用例判 idem_miss。失败原因不是模型没做幂等，是防线把请求本身吞了。

    这不是放松，是把该归谁判的事还回去：
      - "用户要不要建单"是**语义** → 模型判（understand 的 intent）
      - "建单必须可追溯"是**约束** → 规则守（根因 / 证据引用 / 设备存在 / 幂等 四道闸）
    原来让一张词表同时干这两件事，而它只能干后者的一半。
    注意第 2 条只给"许可"，不给"豁免"：引用校验、根因校验照样在后面逐条跑。
    """
    q = _current_question(state)
    if any(kw in q for kw in WRITE_INTENT_KW):
        return True
    return state.get("intent") == "action"


def act(state):
    last = state["messages"][-1]
    out_msgs, evidence, names = [], [], []
    # 计数从"已有条数"续，不从 1 重数：evidence 在 state 里是累加的，
    # act 每次只返回本轮增量，按增量列表长度算，第二轮又会冒出证据 1、2、3。
    # 这个号现在只当 evidence 的稳定 id 用（trace / 报告里对得上哪一条），
    # **不再参与引用校验**——档 C 之后校验按"工具名{指纹}#字段路径"找，
    # 所以"同号对应两条证据、后写覆盖先写"那个问题随 by_no 索引一起消失了。
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
            elif name in WRITE_TOOLS and not _has_write_intent(state):
                # 写意图硬开关。放在归属校验之前：这是"该不该做这件事"，
                # 归属是"能不能对这台设备做"，前者不成立后者没有意义。
                # 被拒后仍走正常的 evidence / trace 记账，模型能看到拒绝理由，
                # 审计里也能看到"模型尝试过建单、被闸拦下"。
                res = {
                    "error": f"用户没有要求建单或上报，本次不执行 {name}",
                    "blocked_by": "write_intent",
                    "hint": (
                        "如果确实需要建单，先向用户确认一遍再调。"
                        "用户没有提出建单/报修/上报时，不要自行建单。"
                    ),
                }
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
                    # 建单前核对引用。工单是唯一会离开本系统的产物，
                    # 只校验"非空"等于允许编造的引用进单——那是发出去之后才暴露的问题。
                    bad_refs = (
                        _unresolvable_refs(args.get("evidence_refs"), state)
                        if name == "create_work_order" else []
                    )
                    if bad_refs:
                        res = {
                            "error": "证据引用无法核对，工单未创建",
                            "problems": bad_refs,
                            "hint": (
                                "把每条 evidence_refs 改成「工具名#字段路径」的指针，"
                                "指向本轮真正查到过的字段。"
                                "不要写「1号工位误判率57.6%」这类自然语言复述，"
                                "也不要引用没有调用过的工具。"
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

        # ev_no 是跨轮累计的绝对号，不是本轮内的序号。它只给审计和报告看，
        # 不参与校验：引用契约用的是工具名+指纹（见 _REF_PAT），模型看不到编号。
        # 之所以还留着递增号，是因为 traces / history 里需要一条稳定的定位键，
        # 否则"第 3 轮那条 query_defect_log"在一轮调了四次时指不清是哪一次。
        ev_no = base + len(evidence) + 1
        # 参数指纹和工具名一起印在 payload 头部：同一工具换个参数再查一遍时，
        # 模型只有看到这个才能明确"我要引的是哪一次"。
        sig = _call_sig(args)
        slim, cut = _payload(res, tool=name, sig=sig)
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
        evidence.append({"tool": name, "args": args, "sig": sig, "result": slim,
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

    先剥掉 _payload 加的 "[工具名] " 前缀——它只是给模型看的身份标记，
    不是数据的一部分。不剥掉，json.loads 会失败，所有引用都被判 ref_bad，
    报出来又是"模型引用写错"，而真凶是包装格式。
    """
    r = e.get("result")
    if isinstance(r, (dict, list)):
        return r
    if not isinstance(r, str):
        return _MISS
    m = _TOOL_TAG.match(r)
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


def _unresolvable_refs(refs, state):
    """建单前核对每一条证据引用是否真的指得到东西。返回问题列表，空 = 全部可解析。

    为什么放在 act 层而不是 create_work_order 里：工具实现拿不到 state，
    只看得到参数。而"这条 ref 指向的字段到底存不存在"必须对着本轮 evidence 才算得出来。
    放在工具里最多校验格式——格式写对、内容是编的，照样通过，工单就带着假证据发出去。
    那是唯一一个会离开本系统的产物，不能只验格式。

    为什么仍然用字符串、不改 schema 成 {tool,sig,path}：
      - 外发格式一变，mock QMS 与真实 QMS 都要跟着改；
      - 模型写对象比写字符串更容易崩（嵌套结构、漏 key）。
    先让"写错会被拒"生效，等模型稳定写出正确指针，再谈结构化。
    这一步的收益已经是全部收益的绝大部分：编造的引用进不了单。
    """
    problems = []
    by_tool = {}
    for e in list(state.get("evidence") or []):
        by_tool.setdefault(e.get("tool"), []).append(e)

    for raw in (refs or []):
        s = str(raw or "").strip()
        if not s:
            continue
        m = _REF_PAT.match(s)
        if not m:
            problems.append(
                f"证据引用「{s}」格式不对：应写「工具名#字段路径」"
                f"（例如 query_defect_log#groups[0].rate），不要写自然语言复述")
            continue
        tool, sig_g, path = m.group(1), (m.group(2) or ""), m.group(3)
        sig = sig_g[1:-1] if sig_g else ""

        cands = by_tool.get(tool)
        if not cands:
            if tool in TOOL_IMPLS:
                problems.append(f"证据引用「{s}」：{tool} 本轮没有调用记录")
            else:
                problems.append(f"证据引用「{s}」：没有名为 {tool} 的工具")
            continue

        # 指纹只收窄不判死，理由与 _verify_claims 一致：
        # 匹配不上可能是参数抄错（真错，后面的路径校验会抓住），也可能是被裁出窗口。
        if sig:
            narrowed = [c for c in cands if _sig_matches(sig, c.get("sig"))]
            if narrowed:
                cands = narrowed

        hit = False
        for c in cands:
            v = _get_path(_load_obj(c), path)
            if v is not _MISS and v not in (None, "", [], {}):
                hit = True
                break
        if not hit:
            problems.append(f"证据引用「{s}」：{tool} 的返回里没有字段 {path}")

    return problems



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


def final_to_text(final):
    """把 final（可能是 dict，也可能是字符串）变成字符串。

    final 的形态在迁移期有两种：写入端已经解析成 dict 的，
    和历史 checkpoint 里仍是 JSON 字符串的。所有"需要字符串"的地方都走这里，
    不要各自取出来就切片或取 len：

      len(dict)    → 静默变成 key 的数量（3、4），报告上看着像个长度
      dict[:300]   → TypeError，节点崩

    前者最坏：它不报错，只是把"结论多长"变成了"结论有几个字段"。
    """
    if isinstance(final, dict):
        return json.dumps(final, ensure_ascii=False)
    return str(final or "")


def _parse_claims(text):
    """从结论里取出结构化断言。返回 dict 或 None（模型没按契约输出）。

    模型有时会把 JSON 包在 ```json ``` 里，先剥掉再解析。
    剥不掉、解析失败、没有 claims 字段，一律返回 None，
    由调用方退回旧的文本解析路径——契约迁移期不能因为一次格式抖动就丢结论。

    **两种输入都接受**：已经是 dict 的（写入端解析过一次，直接存进 final），
    和仍然是字符串的（模型原始输出、历史 checkpoint 里的旧格式）。
    这样 final 从"字符串"改成"dict"可以逐点推进，不必一次改完——
    每改一个写入点，读取端不用跟着动。
    """
    if isinstance(text, dict):
        # 已经是解析好的。_repaired 标记只在字符串路径产生，
        # 这里返回的 dict 不该带它——带了会让 verify 把一次普通解析记成"修复"。
        return text if isinstance(text.get("claims"), list) else None
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
    elif 0 < g <= 1 < t:
        # 单值 claim 会写 unit:"%"，多值 items 的元素没有这个位置，unit 是空的。
        # 于是同一个数两种写法被判成两个数：证据 rate=0.0476，模型写 4.76。
        # 只认 ×100 这一个方向：反方向会把「模型写 0.0476、证据是 4.76」
        # 也救回来，而那种错模型不犯（它抄的是百分数）。
        g *= 100
    return abs(g - t) <= max(tol, abs(t) * 0.01)


# ---------------------------------------------------------------------------
# 派生值：模型自己算出来的数字
#
# 判据只认两种数：从工具返回里抄来的（要给 ref），和算出来的（要给算式）。
# 第三种"既没 ref 也没算式"才是编的。
#
# 为什么必须重算而不是一律放行：放行等于把"我算的"和"我编的"合并成一类，
# 模型很快学会用一句假算式洗白任意数字。
#
# 为什么必须而不是一律判死：判死等于告诉模型"别算"，而很多结论的核心
# 恰恰是比值（20/27 = 74%）。它会把算式删掉、数字照样留在结论里——
# 问题没消失，只是从"能查的"退化成"查不到的"。
#
# 所以：算式可求值 + 结果对得上 → 成立；算式给了但算错 → 判失败（derived_wrong）。
# 前者是能力，后者是错误，两者必须分开计，否则"会算"和"会编"在报表上长得一样。
# ---------------------------------------------------------------------------

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}

# 「（=20/27）」「(= 37 / 200 )」「（=0.5）」
_DERIVED_RE = re.compile(r"[（(]\s*=\s*([^）)]+?)\s*[）)]")
# 算式里只允许这些字符，别的一律当"不是算式"
_EXPR_CHARS = re.compile(r"^[\d\.\+\-\*\/\(\)\s%×÷％]+$")


def _safe_arith(expr):
    """只算 + - * / 和括号，绝不用 eval。

    expr 来自模型输出。在这里 eval 等于把"模型能不能写对 JSON"升级成
    "模型能不能在这台机器上执行任意代码"——一个烂算式就能变成一次入侵。
    白名单 AST 是唯一可接受的做法：认得哪些节点，其余直接拒绝。
    """
    e = (expr or "").replace("×", "*").replace("÷", "/")
    e = e.replace("％", "%").replace("%", "/100")   # 「20%」按 0.2 算
    try:
        node = ast.parse(e, mode="eval").body
    except (SyntaxError, ValueError, TypeError):
        return None

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            x = ev(n.operand)
            return None if x is None else -x
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            a, b = ev(n.left), ev(n.right)
            if a is None or b is None:
                return None
            if isinstance(n.op, ast.Div):
                return None if b == 0 else a / b
            return _BINOPS[type(n.op)](a, b)
        raise ValueError("节点不在白名单")

    try:
        return ev(node)
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def _derive_expr(c, text):
    """取算式：显式 expr 字段优先，其次 text 里形如（=20/27）的括号。

    两种都支持是故意的。prompt 改了，模型不一定立刻照做；而 text 里的算式
    它一直在写（prompt 本来就要求"在 text 里写算式"）。只认显式字段，
    等于把已经存在的那些算式全部作废——修 bug 顺手把旧数据打成错，是另一种 bug。
    """
    expr = str((c or {}).get("expr") or "").strip()
    if expr:
        return expr
    m = _DERIVED_RE.search(text or "")
    if not m:
        return ""
    inner = m.group(1).strip()
    return inner if _EXPR_CHARS.match(inner) else ""


def _verify_derived(c, text, v, unit):
    """派生值重算。返回 (是否成立, 判失败的理由)。

    理由为空  = 没给可用算式（不是错，是没给），调用方按"未溯源"标灰。
    理由非空  = 给了算式但对不上（这是错，判失败）。
    """
    expr = _derive_expr(c, text)
    if not expr:
        return False, ""
    r = _safe_arith(expr)
    if r is None:
        return False, f"算式「={expr}」无法求值"
    try:
        t = float(v)
    except (TypeError, ValueError):
        return False, ""
    # 百分比：结论写 74（unit=%），算式给 20/27 = 0.74 → 先换到同一口径再比
    if unit == "%" and 0 < r <= 1 and t > 1:
        r *= 100
    if abs(r - t) <= max(0.02, abs(t) * 0.01):
        return True, ""
    return False, f"算式「={expr}」得 {r:g}，与写的 {t:g} 不符"


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
        # 出处标记要跟着这句话走。只记在报告里，用户看到的就是一个没有出处的数字——
        # 而"看得到出处"才是这套契约对用户的全部意义。
        # 标的是工具名不是编号：用户看到 [query_device_profile] 能直接对上是哪次查询，
        # 看到 [7] 还得自己去数第七次调了哪个工具。
        ref = str(c.get("ref") or "")
        m = _REF_PAT.match(ref)
        if m:
            # 带指纹的连指纹一起显示：同一工具查了两次，光写工具名
            # 用户还是分不出这句话出自按工位那次还是按缺陷那次。
            mark = f"[{m.group(1)}{m.group(2) or ''}]"
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
            # cite_oob  引的工具在本轮视野内没有返回（多为上下文裁剪所致）→ 标灰放行
            "cite_oob": 0,
            "path_ok": 0, "path_bad": 0, "path_miss": 0,
            # json_bad  模型没按 claims 契约输出 → 整条无法核验，判失败
            # unsourced claim 的 ref 写了 null（有数字、没出处）
            # ref_bad   ref 格式错，或引用的那条证据不是结构化返回
            # cite_cut  那条证据返回体过大被压缩，字段没保留 → 放行标灰
            # json_fixed 结论 JSON 里值内裸引号被修好了，校验照常进行
            "json_bad": 0, "unsourced": 0, "ref_bad": 0, "cite_cut": 0,
            "json_fixed": 0,
            # derived_ok   ref 为空但给了算式，重算对得上 —— 算出来的数，可验
            # derived_bad  ref 为空、给了算式，但算错了 —— 这是真错，判失败
            "derived_ok": 0, "derived_bad": 0,
            # range_ok   区间 claim，两端都能在 ref 指向的那段原文里找到
            # range_bad  区间 claim 但撑不住：ref 指着数值字段，或端点不在该原文里
            "range_ok": 0, "range_bad": 0,
            # ref_ambiguous  同一工具本轮按多种参数各调过，ref 又没写参数指纹，
            #                模型引的可能是另一组的同一个字段——判据不知道指哪次。
            #                这类仍计入失败（伪引用不能放行），但和"值对不上"分开计，
            #                否则报出来的数字会指向错误的地方（实测 A2：报 0.0667≠36.36%，
            #                看着像模型算错，实际是拿 defect_type 那次的候选去比 station）
            "ref_ambiguous": 0,
            # multi_ok   多值 claim（items 数组）：每条「标识+数值」都在返回里找到了对应记录
            # multi_bad  多值 claim 但至少一条配不上：值不在那条记录里，或记录里没这个 key
            "multi_ok": 0, "multi_bad": 0,
            # qual_ok        定性 claim（没有数字、没有 items、没有引文）：ref 指得到
            # qual_bad       定性 claim 但 ref 指不到——没有值可比时，ref 是唯一凭据
            # qual_unsourced 定性 claim 没写出处 → 标灰不判死（同"有数字没出处"）
            "qual_ok": 0, "qual_bad": 0, "qual_unsourced": 0,
            # quote_ok   引文型 claim：模型抄的原文确实在 ref 指向的那段里
            # quote_bad  引文型 claim 但抄错了：原文里找不到这句话
            # bare_conclusion  结论里写了数字、claims 为空 → 没有一个数字可核验
            # svc_fail   整理答案那步模型调用失败：形状合法但内容是半成品
            # ev_mismatch  check 预判用的证据条数 与 verify 终审时的不一致
            #              → check 是拿一批证据放行的、verify 是拿另一批判死的。
            #              程序口径自相矛盾，不是模型的问题，所以只记不判死。
            "quote_ok": 0, "quote_bad": 0, "bare_conclusion": 0,
            "svc_fail": 0, "ev_mismatch": 0}


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


def _verify_quote(c, text, quote, by_tool, stats, blocked, misquoted, fails=None):
    """引文型 claim：模型说"这段原文在这个片段里"，程序回原文核对。

    数字型 claim 问的是"这个数在不在这个字段"，引文型问的是"这句话在不在这份原文"。
    后者才是 knowledge 类结论的主要风险：不是数字编了，而是「手册规定 X」这句话
    手册根本没写——它连数字都可能是对的，只是出处是假的。

    允许引多段：Anthropic 的做法是模型只给位置、程序去文档里切，所以 cited_text
    永远正确；我们让模型自己抄，就必须接受它引两处的情况。按省略号/换行拆开，
    每一段都必须真是原文——引多段可以，编一段不行。

    fails：失败明细列表。misquoted 里那条是"给人看的句子"，fails 里那条是
    "给程序分类的记录"。两处必须是同一次决定——所以统一走 _fail()，
    而不是在两个地方各写一遍。
    """
    def _fail(code, msg):
        misquoted.append(msg)
        if fails is not None:
            fails.append({"code": code, "note": msg})

    ref = c.get("ref")
    if not ref:
        stats["unsourced"] += 1
        blocked.append(text or quote[:40])
        return
    m = _REF_PAT.match(str(ref))
    if not m:
        stats["ref_bad"] += 1
        _fail("ref_format", f"{text}（出处格式错:{ref}，应写 工具名#字段路径）")
        return
    tool, sig_g, path = m.group(1), (m.group(2) or ""), m.group(3)
    sig = sig_g[1:-1] if sig_g else ""
    cands = by_tool.get(tool)
    if not cands:
        if tool in TOOL_IMPLS:
            stats["cite_oob"] += 1
            blocked.append(f"{text}（{tool} 的返回不在本轮可见范围）")
        else:
            stats["ref_bad"] += 1
            _fail("tool_unknown", f"{text}（出处写了不存在的工具名 {tool}）")
        return

    # 指纹只收窄不排除：匹配不上可能是被裁，可能是抄错参数，
    # 两种情况都不该在这里判死；抄错参数的那条，下面的"原文必须在里面"会抓住它。
    if sig:
        narrowed = [c for c in cands if _sig_matches(sig, c.get("sig"))]
        if narrowed:
            cands = narrowed

    # 引文可以在该工具的任一条返回里出现——同一工具多次调用时，
    # 模型记不住"是第几次"，但它抄的那段原文只可能来自其中一次。
    # 所以收集所有候选，逐条试"整段引文都在里面"，任一命中即成立。
    # 只取第一条含该字段的返回就 break，会把"搜了两次手册"变成必然误判。
    hits = []
    any_truncated = False
    for cand in cands:
        obj = _load_obj(cand)
        if obj is _MISS:
            continue
        g = _get_path(obj, path)
        if g is _MISS:
            if cand.get("truncated"):
                any_truncated = True
            continue
        hits.append(g)

    if not hits:
        if any_truncated:
            stats["cite_cut"] += 1
            blocked.append(f"{text}（{tool} 返回体过大已压缩，该字段未保留）")
        else:
            stats["path_miss"] += 1
            _fail("path_missing", f"{text}（{tool} 的返回里没有字段 {path}）")
        return
    if not any(isinstance(g, str) for g in hits):
        # 引文必须有原文可核。ref 指到一个数值，就没有"原文"这东西可引。
        stats["quote_bad"] += 1
        _fail("ref_not_text", f"{text}（ref 指向的不是原文，撑不住一句引文）")
        return
    segs = [s for s in re.split(r"…+|\.{3,}|\n", quote) if s.strip()]
    for g in hits:
        if not isinstance(g, str):
            continue
        if not [s for s in segs if _norm_quote(s) not in _norm_quote(g)]:
            stats["quote_ok"] += 1
            return
    stats["quote_bad"] += 1
    first = next(g for g in hits if isinstance(g, str))
    bad = [s for s in segs if _norm_quote(s) not in _norm_quote(first)]
    _fail("quote_not_found", f"{text}（引文在 {tool}#{path} 里找不到：{bad[0][:28]}）")


def _match_item(item, arr, unit):
    """一条「标识 + 数值」是否能在数组的某条记录里对上。

    这不是"值在数组里出现过"，是"这个值属于这条记录"。差别在 H1 那种形状下是决定性的：
    三个日期三个亮度值 —— 85/80/65 可能各自都在数组里，但配错日期就说明模型没在读数据，
    只是在凑数字。子串匹配 key 是因为模型通常只写日期（"2025-03-12"），
    而 ts 是完整时间（"2025-03-12 09:00:00"）。

    field 省略时，在记录里找任一数值字段等于 value——用于模型不确定字段名的场景；
    指定了 field 就只比那一个，避免"漂移 0.064 对上了 px 也 0.064 的另一条"这种串位。
    """
    if not isinstance(item, dict):
        return False
    val = item.get("value")
    if val is None:
        return False
    key = str(item.get("key") or "").strip()
    field = str(item.get("field") or "").strip()

    for e in arr:
        if not isinstance(e, dict):
            continue
        if key and not any(isinstance(v, str) and key in v for v in e.values()):
            continue
        if field:
            g = e.get(field)
            if g is not None and _value_match(val, g, unit):
                return True
            continue
        if any(isinstance(v, (int, float)) and not isinstance(v, bool)
               and _value_match(val, v, unit) for v in e.values()):
            return True
    return False


def _verify_claims(doc, ev, stats, blocked, misquoted, fails=None):
    """按 claim 逐条校验：一个数字一条，各自带 ref。

    这条路把「引用」从文本解析搬到结构字段，所以三类老毛病结构性消失：
      span      一句三个数字共用一个锚点 —— 想写三个就必须写三条 claim
      写错/没写 混在同一个解析失败里 —— ref 空是没写，ref 指不到是写错
      标灰看不见 只能记在报告里     —— 渲染时把标记跟在数字后面

    fails：失败明细。stats 是 27 个计数字段，半年后加一类失败就得再加一个 key，
    而 key 加完没有人会回头去改 eval / 报告 / UI 的读数——它就慢慢变成第二个 state。
    明细列表不一样：每项 {code, note}，加一类失败只是多一种 code，读的人不用改。
    两处必须同一次决定，所以走 _fail()，不在两个地方各写一遍。
    """
    def _fail(code, msg):
        misquoted.append(msg)
        if fails is not None:
            fails.append({"code": code, "note": msg})

    # 证据按工具名建索引。档 C 之后 ref 是"工具名#字段路径"，
    # 同一个工具在一轮里可能调两次，所以每个工具对应一个列表而不是单条。
    # 校验时逐条尝试，任一命中即算引用成立——模型引的是"那次返回里的这个字段"，
    # 不要求它同时记得是第几次调用。
    by_tool = {}
    for e in ev:
        t = e.get("tool")
        if t:
            by_tool.setdefault(t, []).append(e)

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
            _fail(
                "bare_conclusion",
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
            _verify_quote(c, text, quote, by_tool, stats, blocked, misquoted, fails)
            continue
        # 多值型：`value` 为空但有 items，交给下面的 items 分支逐条配对
        items = c.get("items")
        has_items = isinstance(items, list) and bool(items)
        # 定性型：没有数字、没有 items、没有引文。
        # 以前这里直接 continue —— 等于这句话判据完全不看，连 ref 都不校验。
        # 可没有值可以核的时候，ref 恰恰是唯一凭据：这句话说的是哪个工位、
        # 哪条记录、哪次标定，全在 ref 里。所以它要往下走完整的路径检查，
        # 只是到了比值那一步停下（没有值可比）。
        qual = (v is None and not has_items)
        if not qual:
            stats["qty"] += 1
            if unit == "%":
                stats["pct"] += 1

        ref = c.get("ref")
        if not ref:
            if qual:
                # 定性断言没写出处：和"有数字没出处"同性质——提供了无法核验的
                # 断言，但没说假话。标灰不判死，否则纯推理型 claim 就没法写了。
                stats["qual_unsourced"] += 1
                blocked.append(text or "（空）")
            else:
                # 没有 ref 不等于编的：模型自己算的比值/差值本来就没有字段可指。
                # 给它一条"交算式就能验"的路。否则它只会学会不写算式、
                # 把数字留在结论里——问题没消失，只是从"可查"退化成"查不到"。
                d_ok, d_why = _verify_derived(c, text, v, unit)
                if d_ok:
                    stats["derived_ok"] += 1
                elif d_why:
                    stats["derived_bad"] += 1
                    _fail("derived_wrong", f"{text}（{d_why}）")
                else:
                    stats["unsourced"] += 1
                    blocked.append(text or str(v))
            continue

        m = _REF_PAT.match(str(ref))
        if not m:
            stats["ref_bad"] += 1
            _fail("ref_format",
                  f"{text}（出处格式错:{ref}，应写 工具名#字段路径，如 query_device_profile#maintenance_cycle_days）")
            continue
        tool, sig_g, path = m.group(1), (m.group(2) or ""), m.group(3)
        sig = sig_g[1:-1] if sig_g else ""

        # 按工具名找，不按编号找。编号是位置：_ctx_window 裁掉前几轮之后，
        # 同一份证据的编号不变，但模型看不见它了——然后它会自己补一个"第 1 条"。
        # 工具名是身份，第几轮调都叫同一个名字，裁剪不影响它。
        cands = by_tool.get(tool)
        if not cands:
            if tool in TOOL_IMPLS:
                # 工具真实存在，只是它的返回被裁出了本轮视野
                stats["cite_oob"] += 1
                blocked.append(f"{text}（{tool} 的返回不在本轮可见范围）")
            else:
                # 工具名根本不存在：这是编的出处，和"裁剪"不是一回事
                stats["ref_bad"] += 1
                _fail("tool_unknown", f"{text}（出处写了不存在的工具名 {tool}）")
            continue

        # 参数指纹优先：模型写了 `query_defect_log{group_by=defect}#...`，
        # 就只在"按缺陷分组那次"的返回里找，不再和"按工位那次"混着比。
        #
        # 注意这里**只收窄、不排除**：指纹匹配不上时退回全部候选，按值继续判。
        # 因为匹配不上有两种可能——那次调用被裁出窗口（不该判死），
        # 或者模型把参数抄错了（真错，但值对不上照样会被下面的值比较抓住）。
        # 若在这里直接判 cite_oob，第二种会被当成第一种放过，反而开了条新口子。
        if sig:
            # 按 kv 集合比，不比整串。模型只抄它关心的那一两个（group_by=batch），
            # 实际指纹还带着它没抄的（device_id=...,time_from=...）。
            # 整串精确相等会让这种"写法不全"一条都收窄不到，于是退回全部候选；
            # 而候选数组又只取第一个，最后拿工位表去查批次号——A3/A4 就是这么红的。
            # 收窄的语义不变：模型写出来的 kv 必须全部对上，多写反而更严。
            want = {p.strip() for p in sig.split(",") if p.strip()}
            keep = []
            for c in cands:
                cp = {p.strip() for p in str(c.get("sig") or "").split(",") if p.strip()}
                if want and want <= cp:
                    keep.append(c)
            if keep:
                cands = keep

        # 同一工具多次调用：把所有取得到该字段的返回都收集起来，
        # **再逐个比 value**。只在"取得到"这一步 break，等于拿第一条返回的值
        # 去比模型引的第二条（实测：手册搜了两次，第一条讲标定周期、
        # 第二条讲漂移上限，ref 写一次 search_manual#chunks[0].text 就比错了）。
        hits = []
        any_truncated = False
        for cand in cands:
            obj = _load_obj(cand)
            if obj is _MISS:
                # 不该发生：_payload 已保证输出永远是合法 JSON（超限时降级成
                # _payload_overflow 包装对象）。真到了这里说明有别处写坏了证据。
                continue
            g = _get_path(obj, path)
            if g is _MISS:
                if cand.get("truncated"):
                    any_truncated = True
                continue
            hits.append(g)

        if not hits:
            # 证据被压缩装不下时，字段本身可能就没送进来。这时模型写的路径
            # 也许完全正确，只是那条证据里已经没有它 —— 判模型写错是误判。
            # 和"字段真不存在"分开：前者放行标灰，后者判失败。
            if any_truncated:
                stats["cite_cut"] += 1
                blocked.append(f"{text}（{tool} 返回体过大已压缩，该字段未保留）")
            elif qual:
                # 定性 claim 的 ref 取不到：这句断言就悬空了，没有东西能说明
                # 它针对哪条记录。单独计数——数值路径缺失和定性出处缺失，
                # 一个是"数字没出处"，一个是"结论没落点"，修法完全不同。
                stats["qual_bad"] += 1
                _fail(
                    "qual_ref_missing",
                    f"{text}（定性结论的出处 {tool}#{path} 取不到，"
                    f"无法核对这句话针对哪条记录）")
            else:
                stats["path_miss"] += 1
                # 必须带上工具名。只写"字段不存在:xxx"，读起来就是"模型编了个
                # 不存在的字段"，第一反应会去改 prompt；但这一类的成因可能是
                # 引错了工具，也可能真是字段名错——两种修法完全不同，
                # 文案里必须能分开。
                _fail("path_missing", f"{text}（{tool} 的返回里没有字段 {path}）")
            continue

        if qual:
            # 定性 claim：ref 指得到就算站住，不比较值。
            # 计入 path_ok 是刻意的——它的语义是"引用路径成立"，
            # 定性也在引用，只是没有值可比。不记进去，报告上"可追溯"的比例
            # 就只覆盖数字部分，定性那半截是隐形的。
            stats["qual_ok"] += 1
            stats["path_ok"] += 1
            continue

        # ---- 多值 claim：一条 claim 装 N 个「标识 + 数值」对 ----
        # H1/H3 的形状：三个日期三个亮度值、四次标定四个漂移值，塞进一条 claim，
        # ref 指整个数组。旧结构只能装一个 value，模型只好把数组当 ref、把值写在文本里，
        # 判据拿 list 跟 float 比，一律 path_bad——可模型并不总是编数，
        # 它只是没有地方放"这个值属于哪条记录"。
        #
        # 这里要求的是**配对成立**，不是"值在数组里出现过"。区别在 H1 上很具体：
        # 85/80/65 可能都在数组里，但配错日期就说明没在读数据，只是在凑数字。
        if has_items:
            arrs = [g for g in hits if isinstance(g, list)]
            if not arrs:
                stats["multi_bad"] += 1
                _fail("items_not_array",
                      f"{text}（items 要求 ref 指向数组，实际指向 {hits[0]}，不是多值字段）")
                continue
            # 多个候选数组逐个试，取对得最多的那个。模型引的是"那次返回里的这条记录"，
            # 不是"第几个数组"；指纹收窄不到时是判据自己分不清是哪次调用，
            # 不能让"分不清"变成"模型编数"。
            best_bad, arr = None, None
            for a in arrs:
                b = [it for it in items if not _match_item(it, a, unit)]
                if best_bad is None or len(b) < len(best_bad):
                    best_bad, arr = b, a
            bad = best_bad
            if not bad:
                stats["multi_ok"] += 1
                stats["path_ok"] += 1
                stats["cited_path"] += 1
                stats["cited_ok"] += 1
            else:
                stats["multi_bad"] += 1
                first = bad[0]
                k = first.get("key") if isinstance(first, dict) else first
                vv = first.get("value") if isinstance(first, dict) else None
                _fail(
                    "items_mismatch",
                    f"{text}（items 有 {len(bad)}/{len(items)} 条对不上："
                    f"{k} 对应的值不是 {vv}{unit}，或返回里没有这条记录）")
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
                _fail("range_endpoint", f"{text}（区间端点不是数字:{v}/{vmax}）")
                continue
            if hi < lo:
                stats["range_bad"] += 1
                _fail("range_inverted", f"{text}（区间上下界写反:{lo}>{hi}）")
                continue
            # 文本字段：两端必须同时出现。只找到一端不算——
            # 那说明另一端是模型自己补的，不是原文里的。
            # 多条候选里任一条两端都对上即成立。
            ok = any(isinstance(g, str) and _value_match(lo, g, unit)
                     and _value_match(hi, g, unit) for g in hits)
            if ok:
                stats["path_ok"] += 1
                stats["cited_path"] += 1
                stats["cited_ok"] += 1
                stats["range_ok"] += 1
            elif not any(isinstance(g, str) for g in hits):
                stats["range_bad"] += 1
                _fail("range_ref_numeric",
                      f"{text}（ref 指向数值字段 {hits[0]}，撑不住区间 {lo}~{hi}）")
            else:
                stats["range_bad"] += 1
                _fail("range_mismatch", f"{text}（区间 {lo}~{hi} 与该字段原文对不上）")
            continue

        if any(_value_match(v, g, unit) for g in hits):
            stats["path_ok"] += 1
            stats["cited_path"] += 1
            stats["cited_ok"] += 1
        else:
            # ---- 结构不匹配：ref 指着数组，claim 却是单值 ----
            # A4 就是这样：模型想把"78 属于 2024-11-08 那条记录"塞进一个 value 字段，
            # 判据只能取到整个数组，float(list) 必然失败，于是报
            # 「候选值 [{'ts': ..., 'value': 78}] 都不是 78」——读起来像编数。
            # 但判据自己知道真相：ref 是数组、claim 是单值。这句话该它说，不该让读数的人反推。
            if hits and all(isinstance(g, (list, dict)) for g in hits):
                stats["path_bad"] += 1
                n = len(hits[0]) if isinstance(hits[0], list) else 1
                _fail(
                    "ref_is_collection",
                    f"{text}（ref 指向数组/结构：{tool}#{path} 返回 {n} 条记录，"
                    f"单值 claim 装不下「哪条记录等于多少」。"
                    f"这种配对请改用 items: [{{\"key\": \"<标识>\", \"value\": {v}}}])")
                continue

            # "值对不上"有两种成因，修法完全不同：
            #   ① 模型真编了数               → 改 prompt / 换模型
            #   ② 它引的是另一组的同一个字段 → 让它补参数指纹
            # 判据拿得到多个候选却不知道指哪次时，报出来的是"字段值 0.0667 ≠ 36.36%"，
            # 读起来像①，实际是②。A2 就栽在这：模型查过 group_by=station 却把
            # defect_type 那次的 rate 拿去比，人得翻三个脚本才知道该改哪。
            # 条件只认"没写指纹 + 候选来自多种参数"，两种情况都不占时仍按值判。
            sigs = {cand.get("sig") for cand in cands if cand.get("sig")}
            if not sig and len(sigs) > 1:
                stats["ref_ambiguous"] += 1
                shown = ", ".join(str(g) for g in hits[:3])
                if len(hits) > 3:
                    shown += f" 等 {len(hits)} 个"
                _fail(
                    "ref_ambiguous",
                    f"{text}（引用歧义：{tool} 本轮按 {len(sigs)} 组参数各调过一次，"
                    f"ref 没写参数指纹，候选值 {shown} 都不是 {v}{unit}。"
                    f"要么补指纹指清那一次，要么这个数确实不在任何一次返回里）"
                )
            else:
                stats["path_bad"] += 1
                # 只报 hits[0] 会把"下标写错"读成"编数"：A1 引 groups[7] 想指 11-14
                # （那个在 groups[6]），但第一个候选数组的第 7 项恰好是另一天的 0.0，
                # 报出来就是"字段值 0.0 ≠ 17.65%"——看起来像无中生有。
                # 把候选列全，人才能看出是"指着 0.1875 说 17.65"，即下标错一位。
                shown = "、".join(str(g) for g in hits[:3])
                if len(hits) > 3:
                    shown += f" 等 {len(hits)} 个"
                _fail("value_mismatch", f"{text}（候选值 {shown} 都不是 {v}{unit}）")

    stats["cited"] = stats["cited_ok"]
    stats["ev"] = len(ev)



def verify(state):
    """程序级校验：结论里的每个数量必须能被它引用的那条证据支撑。

    引用身份是**工具名 + 可选参数指纹 + 字段路径**（`query_defect_log{group_by=station}#groups[0].rate`），
    不是"第几条"。判定分四层：

      path_ok    工具名（+指纹）找得到返回，且该字段里确实有这个值   → 通过
      path_bad   工具名找得到、字段也取得到，但值对不上             → 硬拦（明确编造）
      path_miss  工具名对上了，这个字段路径在返回里取不到            → 硬拦（编了路径）
      cite_oob   这个工具在本轮视野内根本没有返回（多为上下文裁剪）   → 记为未引用，不判死
      ref_bad    格式就不是 `工具名#路径`（比如把编号 `7#` 写回来）   → 硬拦

    为什么分层：prompt 遵从率会波动，若把"漏标"也一上来判成编造，
    会造成大面积误报；而误报比漏报更伤——一旦频繁误报，人就会忽略告警。

    为什么 cite_oob 不判死：模型引的工具确实被调过、返回也确实存在，
    只是那一轮已经滑出可见窗口。它没编，只是引了一段自己现在读不到的东西。
    判死等于要求它记住一个程序已经从上下文里删掉的段落。

    覆盖面也扩了：原来只校验 %，一条 420 字的告警解释结论 pct_count=0，
    等于一个数都没看却显示"数字全部可溯源"。现在校验百分比 + 带单位的量，
    设备号 / 告警码 / 日期属于标识符，刻意排除。
    """
    # lookup / knowledge 直接产出 final，不经过 messages；诊断路径才从 assistant 消息取
    # final 可能是 dict（写入端已解析）也可能是字符串（历史 checkpoint / 模型原文）。
    # 这里必须先归一成字符串再判空：dict 没有 .strip()，直接调会 AttributeError。
    raw_final = state.get("final")
    final = _current_final(state)
    # verify 不改 final 的形态：写入端已经是 dict 的，原样带回（下游 result_event
    # 统一序列化）；字符串的也原样带回。只在内存里用归一后的字符串做校验和渲染。
    # 否则"写入端解析成 dict"这一步会被 verify 又变回字符串，等于白做。
    out_final = raw_final if isinstance(raw_final, dict) else final
    no_final = False
    if not final:
        names = ", ".join(state.get("tool_names", []))
        final = f"（已达取证步数上限，已执行工具：{names}，未能形成完整结论）"
        no_final = True

    # 溯源只用本轮和上一轮的工具返回：更早的证据已经被挤出上下文窗口，
    # 模型本轮根本没读到，拿它兜底等于给未溯源数字开后门。
    # 下界必须来自 _ctx_window，和 _context_messages 保留的轮数同源——
    # 这里原本写死 cur_turn - 1，与 CTX_VISIBLE_TURNS=2 数值巧合相等，
    # 改任一处就会让"模型还能看到的返回"与"校验参与匹配的返回"错开一整个轮次。
    # 取证据的规则收在 _visible_evidence 一处：check 也调它，两边口径必须一致，
    # 否则 check 会放行一条模型读不到的旧返回，verify 再判 cite_oob。
    ev = _visible_evidence(state)

    # 冲突从证据里回收，不只靠 knowledge 节点：模型可以直接调 check_doc_conflict，
    # 那条路根本不经过 knowledge。2026-11-18 实测踩过——模型第 8 步直接调了、
    # 返回 has_conflict=true，result 里 conflicts 却是空。绑节点只覆盖走那个节点的路径。
    conflicts = _merge_conflicts(state.get("conflicts"), _conflicts_from_evidence(ev))
    # 查无同理：unknown 的写入口原本全在 lookup / knowledge，而诊断走 plan→act，
    # 那两个节点根本不执行，于是这个字段在主场景上恒为空。也从证据里回收。
    unknown = _merge_unknown(state.get("unknown"), _unknown_from_evidence(ev))

    blocked = []
    # 未溯源 ≠ 伪引用。两个列表装的是两类东西：
    #   blocked    ref 写成 null —— 模型明说"这个数没有直接出处"，标灰放行
    #   misquoted  ref 指到了字段、但那个字段撑不住这个数 —— 判失败
    # 前者可能是算出来的、数出来的、或自己定的目标，程序无从判断，不该判死；
    # 后者是明确声称了出处又对不上，性质不同，混在一起等于判同一个刑。
    # check 抓到的额外问题要并进来：那是"回环后用 ref=null 换通过"这类绕法。
    # 不并进来的话，它就是个写了没人读的字段，而且绕法本身会静默通过。
    misquoted = list(state.get("check_notes") or [])
    # 失败明细：stats 回答"每一类有几个"，这里回答"具体哪几条、为什么、属于哪类"。
    # 口径是"本轮判据判出来的"，不含上面并进来的 check_notes ——那是上一轮回环时
    # 留下的提醒，不是这一次 `_verify_claims` 的判断。
    # 为什么要有它：stats 是 26 个计数字段（`_verify_claims` 运行时另补 cited / ev，
    # 报告里实际 28 个），每加一类失败就得加一个 key，而 key 加完
    # 没人会回头改 eval / 报告 / UI 的读数，半年后它就成了第二个 state。
    # 列表不一样：加一类失败只是多一种 code，读的人不用动。
    cite_fail = []
    stats = _new_stats()

    # check 里也算过一次证据条数（落进 check_ev）。两边调的是同一个
    # _visible_evidence，中间也没有节点会改 evidence，所以正常必须相等。
    # 不等只有一种解释：check 和 verify 看到的证据集不一样——那意味着
    # check 是拿一批证据放行的、verify 是拿另一批判死的，回环那次预判就白做了。
    # 这是程序口径自相矛盾，不是模型编造，所以只记一个数，不判死。
    check_ev = state.get("check_ev")
    if isinstance(check_ev, int) and check_ev != len(ev):
        stats["ev_mismatch"] = 1

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
        _verify_claims(doc, ev, stats, blocked, misquoted, cite_fail)
        # 兜底文案形状上是合法结论（claims 空、conclusion 无数字），
        # 光看形状会静默通过；但那段话里没有任何被整理出来的内容，
        # 拿它当"通过"就是把一次基础设施故障记成了成功。
        # 判失败，但原因写明是"半成品"，不和"引用写错"混为一类。
        # 半成品只进 svc_fail，不进 misquoted。
        # 原来它同时被塞进 misquoted，而 judge 拿 `not misquoted` 判 trace_ok，
        # 于是一次 429 被记成「模型引用写错」，owner 落到 model——
        # 注释写着"不和引用写错混为一类"，实现却把它塞进了同一个列表。
        #
        # 归属依据 Anthropic《Quantifying infrastructure noise》与失败归因三分类
        # （Task's Fault / Agent's Fault / Infrastructure Failure）：
        # 基础设施故障与"任务-模型"这条轴正交，必须单独计数并触发重跑，
        # 混进模型失败率里会让报表失真（该文实测 infra 噪声能吃掉 6 个百分点）。
        svc = str(state.get("svc_fail") or "").strip()
        if svc:
            stats["svc_fail"] = 1
            msg = (f"整理答案的模型调用失败（{svc}），这一条是半成品，不能当结论用")
            cite_fail.append({"code": "svc_fail", "note": msg})
        final_text = preset_text or _render_claims(doc)
        trace.emit("verify", blocked=blocked, misquoted=misquoted,
                   final_len=len(final), **stats)
        # 存解析结果，不再把字符串原样带回去。
        # 以前是"verify 解析一遍 → 把字符串写回 → 下游再解析一遍"，
        # 同一个 doc 在内存里被编解码两次，且下游任何一处忘了 parse 就崩。
        return {"final": doc, "final_text": final_text,
                "blocked": blocked, "misquoted": misquoted,
                "cite_stats": stats, "cite_fail": cite_fail,
                "conflicts": conflicts, "unknown": unknown}

    # 没按契约输出。这里不放行：放行等于"这条没校验"，
    # 而没校验的结论被当成通过，才是真正危险的地方。
    # 唯一例外是步数用尽、压根没形成结论那条，它本来就没声明要溯源。
    stats["json_bad"] = 1
    if not no_final:
        msg = "结论未按 claims 契约输出，引用无法核验"
        misquoted.append(msg)
        cite_fail.append({"code": "json_bad", "note": msg})
    trace.emit("verify", blocked=blocked, misquoted=misquoted,
               final_len=len(final), **stats)
    return {"final": out_final, "final_text": final, "blocked": blocked,
            "misquoted": misquoted, "cite_stats": stats, "cite_fail": cite_fail,
            "conflicts": conflicts, "unknown": unknown}


def _pct(v, digits=2):
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(v)


def _data_block(prof, log, tool_prof, tool_log):
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

    prof_ref = f"{tool_prof}#"
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
        log_ref = f"{tool_log}#"
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
    # "查过了，确实没有"的清单。lookup 里有两处会命中：设备编号不存在、
    # 这台设备没有历史工单。两者都不是错误，是**结论本身**——
    # "这台没建过单"和"我不知道有没有建过"必须能分开，前者是事实，后者是故障。
    unknown = []

    turn = int(state.get("turn") or 1)
    # ev_no 仍然记，但只为审计与报告——引用契约已经改成工具名。
    # 这三个工具的 ref 在下面全部写成 "工具名#字段路径"，和模型走同一套格式，
    # 否则"程序自产的 claims 用一个格式、模型写的 claims 用另一个"，
    # 校验层就得同时理解两种语义，等于把编号那套问题又搬回来。
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
            unknown.append({"kind": "device_not_found", "what": d,
                            "note": "设备库里没有这个编号"})
            continue
        log = query_defect_log(d, group_by="station")
        log_no = base + len(evidence) + 1
        evidence.append({"tool": "query_defect_log",
                         "args": {"device_id": d, "group_by": "station"},
                         "result": log, "turn": turn, "ev_no": log_no})
        names.append("query_defect_log")
        steps += 1
        text, block_claims = _data_block(prof, log,
                                          "query_device_profile", "query_defect_log")
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
                           "ref": "query_work_order#count"})
        else:
            blocks.append(f"### {d} 历史工单\n\n{wo.get('note', '没有工单记录')}")
            # "没建过单"是事实，不是故障。记下来才说得清为什么这一块是空的。
            unknown.append({"kind": "no_work_order", "what": d,
                            "note": wo.get("note") or "没有工单记录"})

    body = "\n\n".join(blocks) if blocks else "没有可用的设备编号。"
    # final 走 claims 契约（给校验层），final_text 保留表格（给人看）。
    # 两份出自同一处取值：拼这一行的时候就知道这个数来自哪个字段，
    # 所以"文本里的数字"和"ref"不可能不一致——它们不是各算一遍。
    #
    # claims 为空时不能还说"列出如下"：结构化消费端（eval / UI / 下一轮摘要）
    # 读的是 conclusion 和 claims 两个字段，一个承诺有内容、另一个是空数组，
    # 这条结论自相矛盾，谁读谁都得先判"到底列没列"。
    # 实测见过这条：用户问"CX-999 有工单吗"，产出
    # `{"conclusion": "按设备档案…列出如下", "claims": []}` —— 说过要列，一条没列。
    if claims:
        tail = (
            "\n\n---\n以上是设备档案、最近 7 天的误判统计与历史工单，**没有做根因判断**。"
            "需要排查原因的话，说一句现象（哪类缺陷、从什么时候开始、哪个班次更重），我再往下查。"
        )
        doc = {
            "conclusion": "按设备档案与最近 7 天的误判统计列出如下，不做根因判断。",
            "claims": claims,
            "advice": [],
        }
    else:
        miss = "、".join(
            str(u.get("what")) for u in unknown
            if u.get("kind") == "device_not_found"
        ) or "、".join(ids)
        tail = "\n\n---\n查不到这台设备，因此没有可列出的数据。"
        doc = {
            "conclusion": f"{miss} 在设备台账里查不到，没有档案、误判统计或工单可列。",
            "claims": [],
            "advice": ["核对现场铭牌或产线看板上的设备编号，用正确编号重新查询"],
        }
    trace.emit("lookup", device_ids=ids, blocks=len(blocks), steps=steps,
               claims=len(claims))
    # final 直接存结构化结果，不再 json.dumps。
    # 序列化只在 result_event 出口做一次——节点里 dumps 一遍、verify 再 parse 回来，
    # 等于把"结构化"这件事只做了一半，还多绕一次文本形态。
    return {"final": doc,
            "final_text": body + tail,
            "evidence": evidence, "tool_names": names, "steps": steps,
            "unknown": unknown}


def _prose_doc(text):
    """程序自己写的声明也走同一个结论结构。

    "服务不可用 / 检索失败 / 手册没收录"这三条是程序的诚实声明，不是模型结论。
    让它们也长成 {"conclusion", "claims": []} 不是为了让校验放行：
    空 claims 本身是有含义的——"这条结论里没有可引用的具体事实"，
    报告上 qty=0 看得见。豁免才是把这件事藏起来。

    返回 dict 而不是 json.dumps：序列化只在 result_event 出口做一次。
    这里以前返回字符串，于是每个调用点都把同一个 dict 编码一遍。
    """
    return {"conclusion": text, "claims": [], "advice": []}


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


def _as_number(v):
    """把"整串就是数字"的字符串转成数字，其余原样返回。

    为什么需要它：check_doc_conflict 的 value 直接取自 Postgres 的 JSONB，
    库里的 typeof 取决于当年入库时怎么写的——线上有 "80"/"65" 这种字符串，
    单测里是数字。读端一旦写 v["value"] > 65 就 TypeError。
    只在 state 视图层统一类型，evidence 里保留工具返回的原值（那是可审计的原始数据）。

    边界刻意收紧：只认"整串是数字"，所以 "90天" / "90~120" / "" 都不动——
    把带单位的字符串截成 90 比留成字符串更危险。
    """
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
    return v


def _conflict_records(conflicts):
    """把 check_doc_conflict 的返回压成进 state 的精简形态。

    完整返回留在 evidence 里（那里是可审计的原始数据）；state 只留
    "哪个参数、哪些文档给了什么值"——判断依据够用，又不把 evidence 复制一份。
    抽成纯函数是为了能单测：原来它内联在 knowledge 里、位于一次模型调用之后，
    想验证"冲突有没有落 state"就得真的花一次 token。
    """
    return [
        {"param": c.get("param_name"),
         "values": [{"source": x.get("source"),
                     "value": _as_number(x.get("value"))}
                    for x in (c.get("conflicts") or [])]}
        for c in conflicts
    ]


def _conflicts_from_evidence(ev):
    """从工具证据里回收文档冲突。

    为什么不只靠 knowledge 节点：模型可以直接调 `check_doc_conflict`
    （plan→act 路径），这时 knowledge 里的 `_conflict_records` 根本不会执行。
    2026-11-18 实测就是这样——模型第 8 步直接调了它、返回 `has_conflict=true`，
    result 事件里 `conflicts` 却是 `[]`。留痕绑在某个节点上，就只覆盖走该节点的路径，
    而主路径恰恰不走它。

    改成扫证据：谁调的、走哪条路都不重要，只要结果在 evidence 里就能收回来。
    """
    out = []
    for e in ev or []:
        if e.get("tool") != "check_doc_conflict":
            continue
        # 必须走 _load_obj：evidence 里存的是 "[工具名{参数}] 正文" 的压缩串，
        # 直接 json.loads 一定失败。失败了就 continue，于是冲突永远收不到——
        # 这是一次静默失效，不是报错，只能靠真实链路的数据才看得出来。
        r = _load_obj(e)
        if not isinstance(r, dict) or not r.get("has_conflict"):
            continue
        out.append({"param": r.get("param_name") or (e.get("args") or {}).get("param_name"),
                    "values": [{"source": x.get("source"), "value": x.get("value")}
                               for x in (r.get("conflicts") or [])]})
    return out


def _merge_conflicts(*lists):
    """按参数名合并冲突记录，先出现的为准。

    knowledge 那份和 evidence 扫出来的那份都要留着：
    前者可能发生在更早的轮次、已滑出可见窗口，只扫证据会漏。
    """
    seen = {}
    for lst in lists:
        for c in lst or []:
            key = c.get("param") or ""
            if key not in seen:
                seen[key] = c
    return list(seen.values())


# 工具报告的「查无」状态 → (归到哪一类缺口, 给人看的一句话)
#
# 提到模块级（原来在 _unknown_from_evidence 里当局部变量），是为了让测试能读它：
# `HelpfulErrorResponseTest` 拿这张表和 `app/tools/hints.py` 的 HINT 交叉校验——
# 这里出现的每个 state 都必须有一条 hint，否则模型只知道"没查到"、
# 不知道"所以别再查"（实测会在查无之后再去查另一个工具）。
#
# bad_args 刻意不在表里：它是参数写错，不是查无。把它记成 unknown，
# 就等于给了模型一个假的拒答理由——"这台设备没有日志"实际是模型把参数拼错了。
STATE_KIND = {
    ("query_work_order", "no_order"): ("no_work_order", "该设备在台账中，但没有任何工单记录"),
    ("query_work_order", "device_not_found"): ("device_not_found", "设备编号不在台账里"),
    ("query_device_profile", "device_not_found"): ("device_not_found", "设备编号不在台账里"),
    ("query_defect_log", "no_defect_log"): ("no_defect_log", "该设备没有误判日志"),
    ("query_calibration", "no_calibration"): ("no_calibration", "该设备没有标定记录"),
    ("query_light_usage", "no_light_record"): ("no_light_record", "该设备没有光源使用记录"),
}


def _unknown_from_evidence(ev):
    """从工具证据里回收"查了、正常返回、结果就是空"的条目。

    和 conflicts 犯的是同一个错，所以用同一个修法：原来 unknown 的三个写入口
    全在 lookup / knowledge 节点里，而诊断意图走 plan→act（route_after_understand
    对 diagnose/action 直接返回 "plan"），根本不经过那两个节点。结果是
    **unknown 在项目主场景上恒为空**——不是"工具没返回空"，是"这些分支没执行"。

    判据只认工具自己声明的 `state`，不猜文案：
      query_work_order     no_order / device_not_found
      query_device_profile device_not_found
      query_defect_log     no_defect_log      （bad_args 是参数写错，不是查无，不收）
      query_calibration    no_calibration
      query_light_usage    no_light_record

    `state` 是 2026-11-18 给这几个工具加的：原来只有 `error` 字符串，
    上游要么不收（unknown 恒空），要么靠文案猜（defect_log 的 error 同时用于
    参数校验失败，猜错就把「参数写错」记成「库里没有」，拒答依据本身是假的）。
    工具自己声明状态，调用方就不用猜。
    """
    out = []
    for e in ev or []:
        tool = e.get("tool")
        args = e.get("args") or {}
        r = _load_obj(e)
        if not isinstance(r, dict):
            continue
        hit = STATE_KIND.get((tool, str(r.get("state") or "")))
        if hit:
            out.append({"kind": hit[0],
                        "what": r.get("device_id") or args.get("device_id") or "",
                        "note": hit[1]})
            continue
        # search_manual 没有 state 字段（它返回的是检索结果而不是台账记录），
        # 空 chunks 就是确定的查无。
        if tool == "search_manual" and not (r.get("chunks") or []):
            out.append({"kind": "manual_empty",
                        "what": r.get("query") or args.get("query") or "",
                        "note": "手册检索返回 0 条"})
    return out


def _merge_unknown(*lists):
    """按 (kind, what) 去重。多轮反复调同一个查不到的工具时，不该记成好几条。"""
    seen = {}
    for lst in lists:
        for u in lst or []:
            key = (u.get("kind") or "", str(u.get("what") or ""))
            if key not in seen:
                seen[key] = u
    return list(seen.values())


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
        # "检索跑通了，但手册里没有" —— 这是结论，不是故障。
        # 归 unknown 不归 svc_fail：svc_fail 的意思是"不知道有没有"，
        # 这里是"确定没有"，两者对下游的动作要求完全不同。
        return {"final": _prose_doc(
            "手册里没有检索到相关内容。可能是这个说法在文档中不存在，"
            "换个说法再问；也可能这条规则确实没有写进手册。"
        ), "evidence": evidence, "tool_names": names, "steps": steps,
            "unknown": [{"kind": "manual_empty", "what": q,
                         "note": "search_manual(top_k=5) 返回 0 条 chunk"}]}

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
    # 冲突记录要落 state，不能只拼成一段人话塞给模型。
    # 原来它算完就只剩 ctx 里那段文字：事后问"这次回答涉及几处文档冲突、
    # 具体是哪两个值打架"，只能去翻 evidence 原文自己拼。
    conflict_records = _conflict_records(conflicts)
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

    # ref 是片段编号（模型只看得见 [1]..[5]），要换成校验层认的工具名 + 字段路径。
    # 纯机械映射，不改模型说了什么：它说"片段 3"，程序翻成"search_manual 的 chunks[2].text"。
    # 越界编号原样留着不修——校验层会记 ref_bad；替它改成"正确"的编号就是合伙作弊。
    #
    # 为什么档 C 删了 evidence 的编号、却留着这个编号：两者作用域不同。
    #   evidence 的号是**跨轮全局累计**，模型跨轮看到的是 session_summary（没有号），
    #   于是它按"我第几个查的"重建一套——这才漂。
    #   chunks 的号是**每次检索重新从 1 排**，而且就印在它这一轮收到的 payload 里，
    #   没有"上一轮的 1"和"这一轮的 1"可以混。
    # 一句话：会漂的不是"编号"这个写法，是"跨轮累计的编号"。别看到 digit 就以为是漏改。
    for c in (doc.get("claims") or []):
        if not isinstance(c, dict):
            continue
        r = str(c.get("ref") or "").strip()
        if r.isdigit():
            i = int(r) - 1
            if 0 <= i < len(chunks):
                c["ref"] = f"search_manual#chunks[{i}].text"

    trace.emit("knowledge", status="ok", chunks=len(chunks),
               claims=len(doc.get("claims") or []))
    # svc_fail 一路传到 verify：兜底文案长得像正常结论（claims 空、conclusion 无数字），
    # 只靠形状分不出来，必须由节点自己说"这一条没跑成"。
    # 同上：上面已经 _parse_claims 解析过一次，这里直接存解析结果，
    # 不要再 dumps 回字符串让 verify 再 parse 一遍。
    return {"final": doc,
            "final_text": _render_claims(doc), "svc_fail": svc_fail,
            "evidence": evidence, "tool_names": names, "steps": steps,
            "conflicts": conflict_records}


def finalize(state):
    """收尾，并给本轮写一句话摘要。

    摘要用规则拼、不再调一次模型：可复现、不花钱、出问题能定位。
    上下文窗口裁剪掉的轮次，就靠这段文字留在 system prompt 里。
    """
    turn = int(state.get("turn") or 1)
    tools = ",".join(state.get("tool_names") or []) or "无"
    user_head = (state.get("user_msg") or "").replace("\n", " ")[:50]
    final = state.get("final", "")
    # 便宜分支"先做后纠"：把假设写在结论最前面，用户一眼能看出我是按什么理解的。
    # 安全前提：verify 已经在 finalize 之前跑完，这里只加前缀、不动 claims，
    # 不会凭空造出一个可引用的事实。也只在 intent_note 非空时加——
    # 置信足够的时候不该有这句，否则每句话都在"我不太确定"，用户会免疫。
    note = (state.get("intent_note") or "").strip()
    if note and isinstance(final, dict) and final.get("conclusion"):
        final = dict(final)
        final["conclusion"] = f"（{note}）\n\n{final['conclusion']}"
    final_head = final_to_text(final).replace("\n", " ")[:80]
    # 结束原因写进跨轮摘要：下一轮模型能看到"上一轮是被预算掐断的"，
    # 就不会把一份半成品结论当成已经查实的事实继续往下推。
    # lookup / knowledge 不走 plan，自然没有这个值，写成"未跑循环"而不是空——
    # 空字符串和"没跑过"在事后统计里长得一样，又是一份分不出来的事实。
    stop = state.get("stop_reason") or "未跑循环"
    # 查无与冲突也进摘要：下一轮模型看到"上一轮那本手册里确实没有这个参数"，
    # 就不会再搜一遍、也不会自己把数补上；看到"有两处取值打架"，就不会随便挑一个当真。
    # 只写前 3 条：摘要是给模型读的一句话，不是清单的副本。
    #
    # 措辞从"查无 X"改成"已排除：X（原因，别再查）"。原来那句只陈述了事实，
    # 没给动作，实测不生效——`probe_unknown_multiturn`：第 1 轮摘要写了
    # 「查无 CX-999」，第 2 轮模型照样去查了一次 profile。
    # 依据 Anthropic《Effective context engineering》：只有事实没有 guidance 时，
    # agent 会 "waste context by misusing tools, chasing dead-ends"。
    # 事实进摘要，动作也要进摘要，否则等于把判断又推回给模型。
    KIND_HINT = {
        "device_not_found": "设备不在台账，别再查它",
        "no_work_order": "工单确实没有，别再查",
        "no_defect_log": "误判日志确实没有，别再查",
        "no_calibration": "标定记录确实没有，别再查",
        "no_light_record": "光源记录确实没有，别再查",
        "manual_empty": "手册里确实没有，别再搜",
    }
    lookups = state.get("unknown") or []
    gap = ""
    if lookups:
        items = []
        for u in lookups[:3]:
            what = str(u.get("what") or u.get("kind") or "")
            why = KIND_HINT.get(str(u.get("kind") or ""), "已确认没有")
            items.append(f"{what}（{why}）")
        gap = "；已排除：" + "、".join(items)
    clashes = state.get("conflicts") or []
    clash = f"；冲突 {len(clashes)} 处" if clashes else ""
    line = (f"\n[{turn}] 用户「{user_head}」→ {state.get('intent') or '?'}；"
            f"工具 {tools}；结束={stop}{gap}{clash}；结论「{final_head}」")
    return {"final": final, "session_summary": line}
