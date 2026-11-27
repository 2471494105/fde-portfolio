"""常见问题目录：客户在中控台上「数得出来」的那几类毛病，以及每一类指向哪张表。

判据按「怎么判」组织（I/J、K、A–H、S1–S5），客户按「我这儿出了什么事」组织。
两者不对齐，就会出现上一轮的尴尬：中控台只有两条事件，客户看不出这台库到底
有多少种典型毛病，也看不出「哪些毛病今天没有」。所以这里登记**问题类别**
（一条 = 一个客户能指着讲的现象），判据只是它的检测手段。

三条口径
1. 类别是登记表，不是运行结果：登记几类、今天命中几类是两件事，页面分开报，
   不许把「登记 9 类」写成「发现了 9 个问题」。
2. 首步动作必须是客户能自己做的一步（补哪张表、让谁确认、看哪张单），
   不写「加强管理」这种谁都能写的话。
3. 样本数一律来自判据台账（`gov_check_run` 最近一次的 `red_count`），
   这里不预置任何数字——预置的数字三个月后必然和库里对不上。

scope 决定它算不算「客户库的问题」（T27 补）：
- client   跑在 fde_client 上，红了就开事件、能点进明细页；
- external 跑在沙箱库（fde_sandbox）或源端样本（CSV）上，不进客户事件流。
  它红的是「靶子/样本有问题」，不是「客户这台库有问题」；上一轮把这两类硬接成
  客户事件，客户点进去会发现那 1 条脏行根本不在自己库里。

targets 是「问题 → 目标数据」的唯一映射（T27）：每张目标表登记定位键、编号前缀、
展示列、改哪一列、能跳哪个页面。判据产出的是编号
（wo_station_identity#WO-2026-0001），明细页只按编号回查，不重算判据。

维护方式：改这里 → python -X utf8 scripts/seed_issues.py 同步到
gov_issue_catalog（页面读表，不读 Python）。
"""

from __future__ import annotations

# 目标表登记：定位键 / 编号前缀 / 展示列 / 改哪一列 / 能跳的页面
TARGET_TABLES: dict[str, dict] = {
    "biz_work_order": {
        "key": "wo_no",
        "collection": "work_orders",
        "prefixes": ["wo_station_identity#"],
        "cols": ["wo_no", "station_no", "prod_line", "station_id", "state", "opened_at"],
        "fix": "补 prod_line（产线A / 产线B / 产线C / 产线D 之一，与 md_station 的产线名一致），复跑后 station_id 才会出来",
        "page": "/app/#work_orders",
    },
    "biz_defect_record": {
        "key": "rec_id",
        "collection": "defect_records",
        "bare": True,  # 判据 K 的编号是裸 rec_id（4/5/6/8/10），没有前缀
        "prefixes": ["defect_station_identity#"],
        "cols": ["rec_id", "captured_at", "equip_no", "work_station", "prod_line", "station_id",
                 "conf_pct", "lot_no", "judge", "is_ok"],
        "fix": "归属：补 prod_line；字段：conf_pct 归一到 0–100、lot_no 非空、captured_at 不晚于今天",
        "page": "/app/#defect_records",
    },
    "biz_alarm": {
        "key": "alarm_id",
        "collection": "alarms",
        "prefixes": ["alarm:"],
        "cols": ["alarm_id", "equip_no", "raised_at", "code", "level", "acked_by", "cleared_at"],
        "fix": "有人接：写 acked_by + 责任人；处理完：写 cleared_at。两列都空 = 这条报警挂在那儿没人管",
        "page": "/v1/alarms",
    },
}

CATALOG: list[dict] = [
    {
        "issue_id": "station_identity",
        "sort_no": 10,
        "short": "归属",
        "name": "工单/缺陷说不清是哪台工位",
        "check_id": "station_contract",
        "owner": "产线计划员",
        "scope": "client",
        "first_step": "让产线计划员把每条记录的「产线」和「工位」填全（产线A–D 之一，工位号写数字）",
        "evidence": "工单与缺陷逐行按 (产线, 工位号) 解析 station_id，解析不出即计入",
        "note": "最大的那一类：一行修不好就一行都算不清，责任人也不认账",
        "targets": ["biz_work_order", "biz_defect_record"],
    },
    {
        "issue_id": "defect_fields",
        "sort_no": 20,
        "short": "字段",
        "name": "缺陷记录字段越界、时间穿越、批次为空",
        "check_id": "defect_fields",
        "owner": "质量工程师",
        "scope": "client",
        "first_step": "让写入设备校三样：置信度不超过 100、批次号不许空、采集时间不许写成明天",
        "evidence": "按 field_rules.json 的 K 系列规则逐条判：越界 / 未来时间 / 空批次",
        "note": "行数少但每一种都能单独讲：越界是写入端没校验，未来时间是设备时钟",
        "targets": ["biz_defect_record"],
    },
    {
        "issue_id": "alarm_ack",
        "sort_no": 25,
        "short": "报警",
        "name": "报警挂着没人接、清了也没人销",
        "check_id": "alarm_ack",
        "owner": "值班人",
        "scope": "client",
        "first_step": "让值班人把每条报警认领、处理完销掉；没人接的先排进今天的班前会",
        "evidence": "biz_alarm 逐行看 acked_by 与 cleared_at：任一为空即这条报警没有闭环",
        "note": "跟工位无关的一类：它不缺归属，缺的是「谁接了」和「什么时候算完」",
        "targets": ["biz_alarm"],
    },
    {
        "issue_id": "quarantine",
        "sort_no": 30,
        "short": "隔离区",
        "name": "脏数据被挡在主表外，没人接手",
        "check_id": "data_contract",
        "owner": "数据管理员",
        "scope": "external",
        "first_step": "让数据管理员按隔离原因回源头改一处，重发后看隔离区归零",
        "evidence": "stg_quarantine 的行数与理由；不算「已处理」，也不算「已丢弃」",
        "note": "外部自检：跑在 fde_sandbox，那 1 条脏行不在客户库里，所以不进客户事件流",
        "targets": [],
    },
    {
        "issue_id": "source_values",
        "sort_no": 40,
        "short": "值域",
        "name": "源端数值越界或量纲错位",
        "check_id": "source_values",
        "owner": "质量工程师",
        "scope": "external",
        "first_step": "让质量工程师先弄清是单位写错还是真越界，再改登记册的允许区间",
        "evidence": "按登记册声明的值域逐列抽样比对（越界算红、允许空却空了算黄）",
        "note": "外部自检：判的是 client_a 的 CSV 样本，不是客户库；结论只说明登记册与样本的关系",
        "targets": [],
    },
    {
        "issue_id": "source_schema",
        "sort_no": 50,
        "short": "结构",
        "name": "源端表结构与登记册对不上",
        "check_id": "source_schema",
        "owner": "数据管理员",
        "scope": "external",
        "first_step": "让数据管理员对一下源端表头的变更单：是漏登记，还是偷偷改了字段",
        "evidence": "源端表头与 data/mapping 的登记逐列比对",
        "note": "外部自检：今天没有样本——这一版源端没改过结构",
        "targets": [],
    },
    {
        "issue_id": "status_policy",
        "sort_no": 60,
        "short": "口径",
        "name": "看板状态词与流程口径对不上",
        "check_id": "status_policy",
        "owner": "车间主管",
        "scope": "client",
        "first_step": "让车间主管确认这一版该用哪套状态词，再改登记册，别改看板",
        "evidence": "按 status_policy.json 的 A–H 逐条核对关单词与在途口径",
        "note": "今天没有样本：口径现在是绿的，不代表以后不会漂",
        "targets": ["biz_work_order", "biz_defect_record"],
    },
    {
        "issue_id": "remediation",
        "sort_no": 70,
        "short": "逾期",
        "name": "整改单到期没关",
        "check_id": "remediation",
        "owner": "车间主管",
        "scope": "client",
        "first_step": "让车间主管看整改单到期没到期：没干完就延期并写原因，干完了等复跑归零",
        "evidence": "gov_remediation_order 里 state 未关且 due_on 早于今天",
        "note": "答的是「有人接了活但没干完」，与数据红分开算",
        "targets": [],
    },
    {
        "issue_id": "sweep_silent",
        "sort_no": 80,
        "short": "静默",
        "name": "判据没人复跑，页面数字已经过期",
        "check_id": "alert_sweep",
        "owner": "值班人",
        "scope": "client",
        "first_step": "让值班人确认巡检还在跑（页面数字超过 24 小时没变，就是它停了）",
        "evidence": "距最近一次运行超过静默阈值（默认 24h），或从没跑过",
        "note": "监控自己静默比数据静默更危险：它会让上面所有数字变成「上周的」",
        "targets": [],
    },
]

# check_id → 类别（事件里按判据找类别；一个判据只归一类，避免一事发两条）
BY_CHECK: dict[str, dict] = {c["check_id"]: c for c in CATALOG}


def category_for(check_id: str) -> dict | None:
    return BY_CHECK.get(check_id)


def is_external(check_id: str) -> bool:
    """跑在沙箱库/源端样本上的判据：结果值得留档，但不能冒充客户库的问题。"""
    c = category_for(check_id)
    return bool(c and c.get("scope") == "external")


def targets_of(check_id: str) -> list[dict]:
    c = category_for(check_id) or {}
    return [{"table": t, **TARGET_TABLES[t]} for t in c.get("targets") or [] if t in TARGET_TABLES]


def summary_counts(items: list[dict], latest: dict[str, int]) -> dict:
    """登记几类、今天命中几类。命中只看样本数 > 0，不看跑过没有；外部自检不计入命中。"""
    hit = [c for c in items
           if c.get("scope") == "client" and latest.get(c["check_id"], 0) > 0]
    ext = [c for c in items if c.get("scope") == "external"]
    return {
        "registered": len(items),
        "client_registered": len([c for c in items if c.get("scope") == "client"]),
        "external_registered": len(ext),
        "hit": len(hit),
        "hit_issue_ids": [c["issue_id"] for c in hit],
    }
