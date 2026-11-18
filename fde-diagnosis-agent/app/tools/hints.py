"""查无类响应的「下一步该做什么」。

依据 Anthropic《Writing effective tools for agents》(2025-09-11)：
> if a tool call raises an error (for example, during input validation), you can
> prompt-engineer your error responses to clearly communicate **specific and
> actionable improvements**, rather than opaque error codes or tracebacks.

只给 `state` 不给 `hint`，模型只知道"这一类没查到"，不知道"所以别再查"。
实测证据（`scripts/probe_unknown_multiturn.py`）：把 `state` 拆出来之后，
模型在「设备不存在」之后仍然去查了 `query_device_profile`，白烧一次调用。
`state` 是给程序和测试用的，`hint` 是给模型用的——两者不能互相替代。

集中成一张表的理由：
- 文案散在 7 个返回点上，改动时容易漏一处，而漏掉的那一处不会有测试报错；
- 表可以被测试和 `app/graph/nodes.py` 的 `STATE_KIND` 交叉校验：
  `STATE_KIND` 里出现的每个 state，这里都必须有对应 hint。
"""

HINT = {
    "device_not_found": (
        "该编号不在设备台账。不要再改查它的工单、标定或光源记录——"
        "那些工具会返回同一个「查无」。请用户核对设备编号。"
    ),
    "no_order": (
        "这台设备确实没有工单，属于结论而非故障。不要重复查询，"
        "也不要编造工单号或按别的设备推断。"
    ),
    "no_defect_log": (
        "这台设备确实没有误判日志，属于结论而非故障。不要重复查询，"
        "也不要按其他设备的比例推算。"
    ),
    "no_calibration": (
        "这台设备确实没有标定记录，属于结论而非故障。不要重复查询，"
        "也不要按其他设备推断。"
    ),
    "no_light_record": (
        "这台设备确实没有光源使用记录，属于结论而非故障。不要重复查询，"
        "也不要按其他设备推断。"
    ),
    # bad_args 不是「查无」——它是参数写错。正因为两种语义共用一个 error 字段，
    # 才需要一条独立的 hint 明确告诉模型"这不是库里没有，是你要改参数"。
    "bad_args": (
        "这是参数写错，不是库里没有数据。请改用允许的取值重试；"
        "不要把它当成「查无」告诉用户。"
    ),
}
