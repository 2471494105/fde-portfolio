"""图状态定义。

累积（operator.add）：messages / evidence —— 跨轮要保留。
session_summary 也累积，但有上限：只留最近 SESSION_SUMMARY_KEEP 行（见 _tail_lines）。
覆盖：其余全部 —— 每轮由对应节点重写，避免上一轮的值污染本轮判断。

steps 覆盖（每个工具调用批次 +1），用于熔断；intake 每轮清零。
intent / device_ids / phenomenon / missing / param_names 是"第一层意图理解"的输出，
understand 判一次，后续节点只读不改。
slots 是跨轮已确认的事实，用合并写：本轮确认了就覆盖，没确认就留着。
"""

import operator
from typing import Annotated, TypedDict


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


def _tail_lines(a, b, keep=SESSION_SUMMARY_KEEP):
    """session_summary 的 reducer：拼接，但只保留最近 keep 行。

    不能直接裁成"最后 keep 个字符"：那是按长度切，会把某一轮摘要的
    后半句留在开头，读起来像一条完整记录，实际上是半句话——比缺行更糟。
    按行切，丢就整行丢。
    """
    lines = [ln for ln in ((a or "") + (b or "")).splitlines() if ln.strip()]
    return "\n".join(lines[-keep:])


class State(TypedDict, total=False):
    user_msg: str
    # intake 用正则抽的候选设备号，只作为 understand 的提示，不是判据
    device_id: str

    # understand 的输出
    intent: str            # diagnose / lookup / knowledge / action / clarify
    device_ids: list       # 模型从原话里抽到的设备号，可能 0 个或多个
    phenomenon: str        # 用户实际说出口的现象/猜测，没说就是空串
    missing: list          # 还缺什么才能继续
    param_names: list      # 用户问到的参数名（用于知识分支查文档冲突）
    intent_reason: str     # 模型或程序给出的判断依据（降级时会被程序改写）
    # 意图判断的自报置信度与处置说明。原来这一步是 temperature=0 的五选一，
    # 没有置信度——判错时下游无从知道它有多虚，只能整条路径跑到底。
    # 现在低于 CONF_ASK 直接降级追问，介于两条线之间则在结论里带免责说明。
    intent_conf: float
    intent_note: str

    # 会话相关（多轮）
    turn: int                              # 第几轮，intake 每轮 +1
    last_question: str                     # 上一轮系统追问的原话
    slots: Annotated[dict, _merge_dict]     # 跨轮已确认的事实：device_id / phenomenon
    # 每轮一句话摘要，裁剪后靠它保留信息。只留最近 SESSION_SUMMARY_KEEP 行——
    # 上限的理由写在 _tail_lines 上面。
    session_summary: Annotated[str, _tail_lines]

    # 累积字段
    messages: Annotated[list, operator.add]
    evidence: Annotated[list, operator.add]

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
    steps: int
    final: str
    # 结论的可读版本：结构化契约（claims）渲染成人话，ref 为空的数字后面带[无出处]。
    # final 保留模型原文，final_text 给人看——两者分开，评测和 UI 各取所需。
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
    "final": "",
    "final_text": "",
    "blocked": [],
    "tool_names": [],
    "plan_retry": 0,
    "stop_reason": "",
    "svc_fail": "",
    "misquoted": [],
    "cite_stats": {},
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
