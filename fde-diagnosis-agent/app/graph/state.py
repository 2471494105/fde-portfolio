"""图状态定义。

累积（operator.add）：messages / evidence —— 跨轮要保留。
session_summary 也累积，但有上限：只留最近 SESSION_SUMMARY_KEEP 行（见 _tail_lines）。
覆盖：其余全部 —— 每轮由对应节点重写，避免上一轮的值污染本轮判断。

steps 覆盖（每个工具调用批次 +1），用于熔断；intake 每轮清零。
intent / device_ids / phenomenon / missing / param_names 是"第一层意图理解"的输出，
understand 判一次，后续节点只读不改。
slots 是跨轮已确认的事实，用合并写：本轮确认了就覆盖，没确认就留着。
"""

from typing import Annotated, TypedDict

# 状态 schema 版本。改了 State 的字段集合就 +1。
#
# 为什么需要：会话是持久的（checkpointer + 固定 thread_id）。改完 State 之后，
# 旧 checkpoint 反序列化出来的 dict **没有新键**。普通读取不受影响（全项目走 .get()），
# 但**累积型字段的 reducer** 会被拿旧值去合并——旧值缺失时传进来是 None，
# 而 `operator.add(None, [msg])` 直接 TypeError，整轮起不来。
#
# 现在兜底交给 _add_list（它接受 None）；state_ver 只负责"这条会话是哪一版 schema、
# 迁移过没有"，是可观测性，不是防线。两者分工别搞反。
STATE_VER = 3


def _add_list(a, b):
    """累积型字段的 reducer：`(a or []) + (b or [])`。

    不能直接用 operator.add：LangGraph 从旧 checkpoint 恢复时，如果这个键
    在当次持久化的 state 里不存在，传进来的是 None，`None + list` 抛
    `TypeError: unsupported operand type(s) for +: 'NoneType' and 'list'`。
    这个错只在"老会话接着用新代码"时出现，新会话永远撞不到——
    正是最容易在演示当天炸、单测永远绿的那类。
    """
    return list(a or []) + list(b or [])


def _merge_dict(a, b):
    out = dict(a or {})
    out.update(b or {})
    return out


# 跨轮摘要保留的行数（一行 = 一轮）。
#
# 取 8 的理由和 CTX_VISIBLE_TURNS=2 配套：上下文里只有最近 2 轮的原文，
# 摘要就是那 2 轮之外唯一的记忆。只留 2 行，"第 3 轮问一句、第 5 轮回来追问"
# 会直接断层；无限留则是每轮都在为很久以前的对话付 token——
# 而 plan 是把整段摘要塞进 prompt 的，这是唯一一个"每轮加一行、从不清理"的字段。
SESSION_SUMMARY_KEEP = 8


def _tail_lines(a, b):
    """session_summary 的 reducer：拼接，但只保留最近 SESSION_SUMMARY_KEEP 行。

    **签名必须严格是两个位置参数。** LangGraph 在 `_add_schema` 里检查
    `_is_field_binop`，签名不是 `(a, b) -> c` 就直接抛
    `ValueError: Invalid reducer signature. Expected (a, b) -> c`，
    **连有默认值的第三个参数也不接受**——图在编译期就挂，整个服务起不来。
    所以 keep 只能读模块常量，不能做成参数。这个坑吃过一次：单测全绿，
    因为没有任何一条测试真的调用过 StateGraph(State)。

    不能直接裁成"最后 keep 个字符"：那是按长度切，会把某一轮摘要的
    后半句留在开头，读起来像一条完整记录，实际上是半句话——比缺行更糟。
    按行切，丢就整行丢。
    """
    lines = [ln for ln in ((a or "") + (b or "")).splitlines() if ln.strip()]
    return "\n".join(lines[-SESSION_SUMMARY_KEEP:])


class State(TypedDict, total=False):
    # schema 版本。intake 每轮写入当前值，用于回答"这条会话跑的是哪一版 State"。
    # 不进 RESET_EACH_TURN：它是会话级元数据，不是本轮事实，跨轮必须保留。
    state_ver: int
    user_msg: str
    # intake 用正则抽的候选设备号，只在 understand 那一次当 prompt 提示。
    # **不是 device_ids[0] 的副本**——来源不同（正则 vs 模型），用处不同（提示 vs 判据）。
    # understand 之后不再写这个字段：下游一律读 device_ids，写回一份会让
    # "这台设备是哪台"有两个来源，改一个忘一个。
    device_id: str

    # understand 的输出
    intent: str            # diagnose / lookup / knowledge / action / clarify
    device_ids: list       # 模型从原话里抽到的设备号，可能 0 个或多个
    # 现象没有顶层字段：它只活在 slots["phenomenon"]。这里曾存过一份同值副本，
    # understand 每轮写两处——写漏一处，clarify 与 plan 就会看到两个版本。
    missing: list          # 还缺什么才能继续
    # "查过了，库里确实没有"——和 missing 是两件事，不能混：
    #   missing    还缺信息，所以要追问用户（缺的是**用户能给的**东西）
    #   unknown    已经查了工具，工具也正常返回，但结果就是空（缺的是**库里没有**）
    #   svc_fail   工具挂了，连"有没有"都不知道（缺的是**系统可用性**）
    # 三者决定的动作完全不同：追问 / 拒答或说明缺口 / 重试或报故障。
    # 原来只有 missing 和 svc_fail，中间这一档没有位置——"手册里没检索到"
    # 只能写进 final_text 的一句人话，报告上只剩 qty=0，看不出是"查无此物"
    # 还是"模型压根没写 claim"。这正是"拒答"和"编造"之间那道必须留下的痕。
    # 每项：{"kind": 原因码, "what": 查了什么, "note": 为什么算没有}
    unknown: list
    # 本轮查到的文档冲突。param_names 是"要查哪些参数"（输入），这里是
    # "查出了什么冲突"（输出），两者不能合成一个字段。
    # 原来冲突只拼成一段人话塞进给模型的 ctx，算完 state 里没有痕迹——
    # 事后问"这次回答涉及几处冲突、哪两个值打架"只能翻 evidence 原文。
    # 每项：{"param": 参数名, "values": [{"source": 文档, "value": 取值}]}
    # 完整 check_doc_conflict 返回仍在 evidence 里，这里只留判断依据。
    conflicts: list
    param_names: list      # 用户问到的参数名（用于知识分支查文档冲突）
    intent_reason: str     # 模型或程序给出的判断依据（降级时会被程序改写）
    # 意图判断的自报置信度与处置说明。原来这一步是 temperature=0 的五选一，
    # 没有置信度——判错时下游无从知道它有多虚，只能整条路径跑到底。
    # 现在低于 CONF_ASK 直接降级追问，介于两条线之间则在结论里带免责说明。
    intent_conf: float
    intent_note: str
    # 这一轮的意图是谁定的：`rule:<规则名>` 还是 `model`。
    # 和 intent_conf 分开看：conf 是模型自报的把握，route_by 是**实际生效的判断来源**。
    # 有它才能回答"这条被规则钉死的用例，模型本来会判成什么、判错了没有"——
    # 也就是规则的真实误伤率。只记进 trace 不够：run_eval 传 trace_run=False，
    # 评测链不落 runs/，那个字段在报告里等于不存在（记了没人拿得到 = 没记）。
    route_by: str

    # 会话相关（多轮）
    turn: int                              # 第几轮，intake 每轮 +1
    last_question: str                     # 上一轮系统追问的原话
    slots: Annotated[dict, _merge_dict]     # 跨轮已确认的事实：device_id / phenomenon
    # 每轮一句话摘要，裁剪后靠它保留信息。只留最近 SESSION_SUMMARY_KEEP 行——
    # 上限的理由写在 _tail_lines 上面。
    session_summary: Annotated[str, _tail_lines]

    # 累积字段
    messages: Annotated[list, _add_list]
    evidence: Annotated[list, _add_list]

    # 本轮字段（每轮重写）
    tool_names: list
    blocked: list
    # 伪引用：ref 指向了一次真实发生过的调用，但那个字段里没有这个数。
    # 和 blocked 分开，是因为 blocked 里的多数值是"数出来但程序认不出"，
    # 两者不该判同一个刑——前者标灰放行，后者判失败。
    misquoted: list
    # verify 的分层统计：qty / pct / cited_ok / path_ok / path_bad / path_miss /
    # cite_oob / ref_bad / cite_cut / unsourced / quote_ok / quote_bad /
    # range_ok / range_bad / bare_conclusion / svc_fail（完整清单见 nodes._new_stats）
    cite_stats: dict
    # 引用失败的明细，每项 {"code": 失败类型, "note": 给人看的那句话}。
    # cite_stats 是"每类几个"（27 个计数字段），这里是"具体哪几条、为什么"。
    # 为什么另开一列而不是继续往 cite_stats 加 key：计数字段每加一类就要加一个 key，
    # 而 key 加完没人会回头改 eval / 报告 / UI 的读数——半年后 cite_stats 就成了第二个 state。
    # 明细列表加一类失败只是多一种 code，读的人不用改。
    # 口径：只装本轮 verify 判出来的，不含上一轮回环留下的 check_notes。
    cite_fail: list
    steps: int
    # 结构化结论：{"conclusion": str, "claims": [{text, value, unit, ref}], "advice": [str]}。
    # 也允许 JSON 字符串——历史 checkpoint 里存的就是字符串，final_to_text 两种都收。
    # 早期这里写的是 `final: str`、注释说"保留模型原文"，而四处写入端当时已经全写 dict。
    final: dict | str
    # 结论的可读版本：结构化契约（claims）渲染成人话，ref 为空的数字后面带[无出处]。
    # final 存结构、final_text 存渲染——评测读前者，UI 读后者。
    final_text: str
    # 因"只写了计划没真调用"而退回重试的次数，防止死循环
    plan_retry: int
    # check 的回环计数与路由动作。verify 放在循环外时是"判完就终局"，
    # 模型没有改的机会；回环一次是把"哪条规则、为什么失败"回喂过去（见 check 的注释）。
    check_retry: int       # 已经回环几次，上限 MAX_CHECK_RETRY
    check_action: str      # retry / pass，供 route_after_check 读，不靠推断
    check_notes: list      # check 抓到的、需要并进最终判定的问题（如"拿 null 换通过"）
    check_unsourced0: int  # 回环前的未溯源条数，用来判断"是不是改成 null 蒙过去"
    # check 预判时用的证据条数。以前它只写不读——注释写了"两边取的条数一旦不同，
    # check 放行、verify 判死"这个担忧，代码里却没有人比对过，等于一个半成品。
    # 现在 verify 会拿它和 len(_visible_evidence(state)) 比一次，不等就记 ev_mismatch。
    check_ev: int
    # 本轮为什么停：need_more / enough / budget_exhausted，由 plan 写入（纯推导，不花 token）。
    # 和 steps 必须同时存在——steps 只说"用了几次"，说不出"是额度用完了，还是判断够了"。
    # 这两件事一个字都不能混：前者是半成品结论，后者是可交付结论，可信度差着量级。
    stop_reason: str
    # 整理答案那步的模型调用失败（异常类名）。只由 knowledge 写入、verify 读取。
    # 不写在 cite_stats 里，是因为它根本不是引用问题：它的形状和正常结论一样
    # （claims 空、conclusion 无数字），只有产出它的节点知道"这一条没跑成"。
    svc_fail: str


# 每轮必须重置的字段与它们的初始值。
#
# **为什么放在这里，而不是写在 intake 里**：这张表就是"本轮字段"的唯一清单。
# 原来它散在 intake 的 return 字面量里，靠人记得加；漏一个的代价是上一轮的值
# 污染本轮，而指标全绿——只有人看文本才知道答的不是同一件事。
# 现在加字段只需要在 State 里声明 + 在这里登记，漏登记的检查点是
# "这个字段在本轮被写过吗"（tests 里有一条断言两者一致）。
#
# 下面几句是四次真实事故的尸检，跟着字段走：
#   final_text —— verify 里 final_text = preset_text or _render_claims(doc)。
#       上一轮留了值，这一轮就会被上一轮的文本盖掉，而 final（claims）是本轮的。
#       三轮探针撞上过：轮2 走 knowledge 留了文本，轮3 走 diagnose，
#       UI 上是"问诊断、答手册"，claims 却全是本轮的、校验还是 ok。
#   stop_reason —— lookup / knowledge 这类路径根本不跑 plan，不清就会把上一轮
#       诊断留下的停止原因，当成本轮的结论报出去。
#   svc_fail —— 这一轮走 lookup 或 clarify（不碰 knowledge）时，上一轮留下的
#       svc_fail 会被 verify 当成本轮的失败，凭空多一条半成品。
#   check_notes —— 会被 verify 并进 misquoted（"回环后用 null 换通过"那条），
#       不清就会让第 1 轮的回环问题在第 2 轮再判一次失败。
#
# 不在表里的三个累积字段（messages / evidence / session_summary）和多轮合并的
# slots：它们的语义是"保留"，不是"重置"。
RESET_EACH_TURN = {
    "steps": 0,
    # understand 每轮重新判定路由来源。不清的话第 1 轮被规则命中的用例，
    # 第 2 轮明明走的是模型，报告上还写着 rule:xxx——误伤率统计会凭空多出一条。
    "route_by": "",
    "final": "",  # 必须是空串不是 {}：final_to_text({}) 返回字符串 "{}"，_current_final 会当它非空
    "final_text": "",
    "blocked": [],
    "tool_names": [],
    # unknown 是本轮"查过了确实没有"的清单，必须每轮清——
    # 不清的话第 1 轮"手册里没这条"，第 2 轮问别的东西还挂着，
    # 报告上会变成"这一轮也查无此物"。
    "unknown": [],
    "conflicts": [],
    "plan_retry": 0,
    "stop_reason": "",
    "svc_fail": "",
    "misquoted": [],
    "cite_stats": {},
    "cite_fail": [],
    "check_notes": [],
    "check_retry": 0,
    "check_action": "",
    "check_unsourced0": 0,
    # 初值是 None 而不是 0：0 是"check 跑了、看到 0 条证据"的合法结果，
    # None 才是"本轮没跑 check"。verify 靠这个区分，否则 lookup / knowledge
    # 这类不经过 check 的路径会被误记一条 ev_mismatch。
    "check_ev": None,
}


def fresh_reset_values():
    """取一份 RESET_EACH_TURN 的副本。

    list / dict 必须复制：直接返回同一份实例的话，所有轮次共享一个对象，
    下游 append 一次，下一轮一开跑就带着上一轮的内容——正是这张表要防的事。
    """
    return {k: (v.copy() if isinstance(v, (list, dict)) else v)
            for k, v in RESET_EACH_TURN.items()}
