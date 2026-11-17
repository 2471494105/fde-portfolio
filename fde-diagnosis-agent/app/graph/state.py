"""图状态定义。

累积（operator.add）：messages / evidence / session_summary —— 跨轮要保留。
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


def _concat(a, b):
    return (a or "") + (b or "")


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
    session_summary: Annotated[str, _concat]  # 每轮一句话摘要，裁剪后靠它保留信息

    # 累积字段
    messages: Annotated[list, operator.add]
    evidence: Annotated[list, operator.add]

    # 本轮字段（每轮重写）
    tool_names: list
    blocked: list
    # 伪引用：[k] 指向了真实存在的那条证据，但该证据里没有这个数。
    # 和 blocked 分开，是因为 blocked 里的多数值是"数出来但程序认不出"，
    # 两者不该判同一个刑——前者标灰放行，后者判失败。
    misquoted: list
    # verify 的分层统计：qty / pct / cited_ok / cited_bad / cite_oob / uncited / cited / ev
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
    # 本轮为什么停：need_more / enough / budget_exhausted，由 plan 写入（纯推导，不花 token）。
    # 和 steps 必须同时存在——steps 只说"用了几次"，说不出"是额度用完了，还是判断够了"。
    # 这两件事一个字都不能混：前者是半成品结论，后者是可交付结论，可信度差着量级。
    stop_reason: str
