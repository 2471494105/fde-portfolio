"""常见问题目录：客户在中控台上「数得出来」的那几类毛病。

为什么要有这份目录
----------------
判据是按「怎么判」组织的（I/J、K、A–H、S1–S5），客户是按「我这儿出了什么事」
组织的。两者不对齐，就会出现上一轮的尴尬：中控台上只有两条事件，客户看不出
这台库到底有多少种典型毛病，也看不出「哪些毛病今天没有」——没有的那部分，
恰恰是他判断这套系统有没有覆盖他的日常的依据。

所以这里登记**问题类别**（一条 = 一个客户能指着讲的现象），判据只是它的
检测手段。一条类别可以有多道判据，也可以暂时没有样本；**没有样本不等于没有
判据**，页面上分开写。

三条口径（与被 events 复用的 alerts 模块一致）
1. 类别是登记表，不是运行结果：登记 8 类、今天命中几类是两件事，页面分开报，
   不许把「登记 8 类」写成「发现了 8 个问题」。
2. 首步动作必须是客户能自己做的一步（补哪张表、让谁确认、看哪张单），
   不写「加强管理」这种谁都能写的话。
3. 样本数一律来自判据台账（`gov_check_run` 最近一次的 `red_count`），
   这里不预置任何数字——预置的数字三个月后必然和库里对不上。

维护方式：改这里 → `python -X utf8 scripts/seed_issues.py` 同步到
`gov_issue_catalog`（页面读表，不读 Python）。
"""

from __future__ import annotations

# sort_no 决定页面顺序：越靠前越是「客户一说就懂、一修就能看见」的那类
CATALOG: list[dict] = [
    {
        "issue_id": "station_identity",
        "sort_no": 10,
        "short": "归属",
        "name": "工单/缺陷说不清是哪台工位",
        "check_id": "station_contract",
        "owner": "产线计划员",
        "first_step": "在工单里补 prod_line（与 md_station 的产线名一致），再复跑判据 I",
        "evidence": "工单与缺陷逐行按 (产线, 工位号) 解析 station_id，解析不出即计入",
        "note": "最大的那一类：一行修不好就一行都算不清，责任人也不认账",
    },
    {
        "issue_id": "defect_fields",
        "sort_no": 20,
        "short": "字段",
        "name": "缺陷记录字段越界、时间穿越、批次为空",
        "check_id": "defect_fields",
        "owner": "质量工程师",
        "first_step": "让写入端把 conf_pct 归一到 0–100、批次号必填，再复跑判据 K",
        "evidence": "按 field_rules.json 的 K 系列规则逐条判：越界 / 未来时间 / 空批次",
        "note": "行数少但每一种都能单独讲：越界是写入端没校验，未来时间是设备时钟",
    },
    {
        "issue_id": "quarantine",
        "sort_no": 30,
        "short": "隔离区",
        "name": "脏数据被挡在主表外，没人接手",
        "check_id": "data_contract",
        "owner": "数据管理员",
        "first_step": "按隔离区理由（如 ts_in_future）回源端改一处，重发后看隔离区归零",
        "evidence": "stg_quarantine 的行数与理由；不算「已处理」，也不算「已丢弃」",
        "note": "最容易被漏的一类：它不报错、不进主表，报表上完全看不见",
    },
    {
        "issue_id": "source_values",
        "sort_no": 40,
        "short": "值域",
        "name": "源端数值越界或量纲错位",
        "check_id": "source_values",
        "owner": "质量工程师",
        "first_step": "先确认是单位写错还是真越界，再改登记册的允许区间或改写入端",
        "evidence": "按登记册声明的值域逐列抽样比对（越界算红、允许空却空了算黄）",
        "note": "置信度 150 / −10 这种，多半是百分比与小数混写",
    },
    {
        "issue_id": "source_schema",
        "sort_no": 50,
        "short": "结构",
        "name": "源端表结构与登记册对不上",
        "check_id": "source_schema",
        "owner": "数据管理员",
        "first_step": "对一下源端表头的变更单：是漏登记，还是源端偷偷改字段",
        "evidence": "源端表头与 data/mapping 的登记逐列比对",
        "note": "今天没有样本：这一版源端没改过结构",
    },
    {
        "issue_id": "status_policy",
        "sort_no": 60,
        "short": "口径",
        "name": "看板状态词与流程口径对不上",
        "check_id": "status_policy",
        "owner": "车间主管",
        "first_step": "让车间主管确认这一版该用哪套状态词，再改登记而不是改看板",
        "evidence": "按 status_policy.json 的 A–H 逐条核对关单词与在途口径",
        "note": "今天没有样本：口径现在是绿的，不代表以后不会漂",
    },
    {
        "issue_id": "remediation",
        "sort_no": 70,
        "short": "逾期",
        "name": "整改单到期没关",
        "check_id": "remediation",
        "owner": "车间主管",
        "first_step": "看整改单的 due_on 与覆盖行数：没干完就延期并写原因，干完了等复跑归零",
        "evidence": "gov_remediation_order 里 state 未关且 due_on 早于今天",
        "note": "答的是「有人接了活但没干完」，与数据红分开算",
    },
    {
        "issue_id": "sweep_silent",
        "sort_no": 80,
        "short": "静默",
        "name": "判据没人复跑，页面数字已经过期",
        "check_id": "alert_sweep",
        "owner": "值班人",
        "first_step": "跑一次 scripts\\patrol.py（一条命令：判据 → 事件 → 自检）",
        "evidence": "距最近一次运行超过静默阈值（默认 24h），或从没跑过",
        "note": "监控自己静默比数据静默更危险：它会让上面所有数字变成「上周的」",
    },
]

# check_id → 类别（事件里按判据找类别；一个判据只归一类，避免一事发两条）
BY_CHECK: dict[str, dict] = {c["check_id"]: c for c in CATALOG}


def category_for(check_id: str) -> dict | None:
    return BY_CHECK.get(check_id)


def summary_counts(items: list[dict], latest: dict[str, int]) -> dict:
    """登记几类、今天命中几类。命中只看样本数 > 0，不看「跑过没有」。"""
    hit = [c for c in items if latest.get(c["check_id"], 0) > 0]
    return {
        "registered": len(items),
        "hit": len(hit),
        "hit_issue_ids": [c["issue_id"] for c in hit],
    }
