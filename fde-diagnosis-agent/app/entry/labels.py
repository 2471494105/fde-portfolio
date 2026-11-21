"""平台无关的人话翻译层：字段名 / 枚举 / 工具名 / 引用标签 -> 现场能读的写法。

从 app/web/server.py 抽出来（DOC-37 W1）。Web 与 IM 两个入口共用这一份，
避免"同一份结论在两个入口上写法不一样"。

注意：本文件与 server.py 都不在 app/meta.py 的 BEHAVIOR_FILES 里——
这里是展示层，不参与模型行为，也不参与评测判定，改它不动 code_hash。
"""

import json
import re

def _as_obj(res):
    """把工具返回值归一化成对象，再往下算摘要。

    这里有两个坑叠在一起：
    \u2460 有些工具（如 query_device_profile）返回的是**已经序列化好的 JSON 字符串**，
       直接 json.dumps 会套第二层引号，前端 parse 出来是 str 而不是 dict；
    \u2461 evidence 里存的 result 不是原始对象，而是模型当时看到的那份带签名文本
       `[query_device_profile{device_id=CX-103}] {"device_id": ...}`。
       签名是给模型和审计看的，字段表只能从它后面的 JSON 算，所以先剥签名。
    两处不处理，界面上就只剩"原始 JSON 片段"——2026-11-20 现场反馈的就是这个现象。
    """
    if not isinstance(res, str):
        return res
    s = res.strip()
    m = re.match(r"^\[.+?\]\s*([\{\[].*)$", s, re.S)
    if m:
        s = m.group(1).strip()
    else:
        # 签名里带方括号参数时上面的懒惰匹配会失手，退一步按 "] {" 切
        i = s.rfind("] ")
        if i > 0 and s[i + 2:].lstrip()[:1] in "{[":
            s = s[i + 2:].strip()
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except Exception:
            return res
    return res


def _summarize(res):
    """把工具返回压成一句人话。

    为什么不交给前端解析：前端拿到的是 json.dumps(res)[:600]，600 字一截就不是合法
    JSON 了，parse 必然失败，于是每条都只能显示"太长"。摘要必须在对象还被完整持有的
    这一侧算出来。
    """
    res = _as_obj(res)
    if isinstance(res, dict) and res.get("error"):
        return "错误：" + str(res["error"])
    if isinstance(res, list):
        return f"{len(res)} 条记录"
    if isinstance(res, dict):
        parts = []
        lists = [(k, v) for k, v in res.items() if isinstance(v, (list, tuple))]
        if lists:
            k, v = max(lists, key=lambda kv: len(kv[1]))
            parts.append(f"{k} {len(v)} 条")
            # 报字段名没用，现场人员想知道的是"第一条长什么样"
            if v and isinstance(v[0], dict):
                k0, v0 = list(v[0].items())[0]
                # 首条的值可能是一整段手册正文，必须截
                s0 = v0[:24] + "…" if isinstance(v0, str) and len(v0) > 24 else v0
                parts.append(f"首条 {k0}={s0}")
        for k, v in res.items():
            if isinstance(v, (int, float, bool)):
                parts.append(f"{k}={v}")
            elif isinstance(v, str) and len(v) <= 40:
                parts.append(f"{k}={v}")
            if len(parts) >= 3:
                break
        return "　".join(parts) if parts else "空结果"
    if isinstance(res, str):
        return res[:45] + ("…" if len(res) > 45 else "")
    return str(res)[:45]


# ============ 人话翻译层 ============
#
# 面向现场的展示转换都在这里做，不散到工具实现里：
#   工具返回的字段名（device_id / group_by / false_positive_rate）是给模型和校验层看的，
#   现场工程师看到 "device_id=CX-103" 等于没看到。摘要和字段表必须拿**完整对象**来算，
#   前端只有 json.dumps(...)[:600] 的残片，parse 必然失败（见 _summarize 的注释）。
#
# 注意：本文件不在 app/meta.py 的 BEHAVIOR_FILES 里，改它不动 code_hash：
# 它是展示层，不参与模型行为，也不参与评测判定。

INTENT_CN = {
    "diagnose": "诊断故障",
    "lookup": "查设备档案",
    "knowledge": "查手册知识",
    "action": "执行动作（建单 / 派工）",
    "refuse": "拒答（超出授权或范围）",
    "clarify": "追问补充信息",
}


def intent_cn(v) -> str:
    """意图枚举转中文。认不出的原样返回——宁可露出英文，也不要编一个中文意思。"""
    return INTENT_CN.get((v or "").strip(), "")


# 工具名也要说人话：query_device_profile 对现场是天书。
# 认不出的原样返回，审计时仍能对上原始工具名（前端 title 里也保留原名）。
TOOL_CN = {
    "query_device_profile": "查设备档案",
    "query_defect_log": "查误判记录",
    "query_calibration": "查标定记录",
    "query_light_usage": "查光源使用",
    "search_manual": "查手册",
    "check_doc_conflict": "查文档冲突",
    "query_work_order": "查工单",
    "create_work_order": "建工单",
}


def tool_cn(name) -> str:
    return TOOL_CN.get((name or "").strip(), name or "")


FIELD_CN = {
    "device_id": "设备编号", "state": "查询状态", "line": "产线", "station": "工位",
    "camera_model": "相机型号", "lens_model": "镜头", "light_model": "光源型号",
    "install_date": "安装日期", "maintenance_cycle_days": "保养周期", "log_count": "误判记录总数",
    "latest_log_at": "最近一次误判",
    "time_from": "统计起点", "time_to": "统计终点", "group_by": "分组维度",
    "total": "误判总数", "false_positive_count": "误判条数", "false_positive_rate": "误判率",
    "groups": "分组明细", "samples": "抽样明细", "key": "分组", "count": "条数",
    "false_positive": "其中误判", "rate": "误判率",
    "ts": "时间", "defect_type": "缺陷类型", "confidence": "置信度", "batch": "批次",
    "records": "标定记录", "last_calibration_at": "最近标定", "days_since_last": "距上次标定",
    "drift_trend": "漂移趋势", "drift_px": "漂移(px)", "drift_mm": "漂移(mm)",
    "result": "结果", "operator": "操作人",
    "cumulative_hours": "累计点亮", "rated_life_hours": "额定寿命", "over_life_hours": "已超寿命",
    "remaining_hours": "剩余寿命", "brightness_set": "当前亮度设定", "last_replaced_at": "上次更换",
    "brightness_history": "亮度调整历史", "value": "亮度值", "degradation_estimate": "衰减判断",
    "scope": "查询范围", "orders": "工单", "order_id": "工单号", "root_cause": "根因",
    "root_cause_code": "根因归类", "severity": "严重度", "status": "状态",
    "assignee_group": "派给", "created_at": "建单时间", "actions": "处置动作",
    "evidence_refs": "依据引用", "note": "说明",
    "query": "检索词", "chunks": "命中片段", "text": "正文", "source_doc": "来源文档",
    "doc_type": "文档类型", "section": "章节", "score": "相似度",
    "param_name": "参数名", "has_conflict": "是否存在冲突", "value_count": "取值种类",
    "conflicts": "冲突明细", "source": "来源", "likely_authoritative": "可能权威的依据",
    "normalized_from": "参数名归一化自", "limit": "条数上限", "top_k": "取前几条",
    "error": "错误",
}

# 同名不同义的字段：count 在分组表里是"条数"，在工单查询里是"工单数"
FIELD_OVERRIDE = {
    "query_work_order": {"count": "工单数"},
    # 113 是区间内记录总数，27 才是其中被判误判的——两个都叫"误判"会看不明白
    "query_defect_log": {"total": "记录总数", "false_positive_count": "其中误判"},
    # 冲突明细里的 value 是"这份文档给的取值"，不是亮度值
    "check_doc_conflict": {"value": "取值"},
}

# 每条工具的主列表：不按"哪个最长"选，否则 defect_log 的 5 条抽样会盖掉 3 条分组
PREFERRED_LIST = {
    "query_device_profile": None,
    "query_defect_log": "groups",
    "query_calibration": "records",
    "query_light_usage": "brightness_history",
    "search_manual": "chunks",
    "check_doc_conflict": "conflicts",
    "query_work_order": "orders",
    "create_work_order": None,
}

# 这几句是给现场的操作指引，截断就把结论截没了（"不要自行挑选"就在后半句）
FULL_TEXT_KEYS = {"note", "degradation_estimate", "error", "recommendation"}

# 列表行的字段优先级：先放人最关心的，凑满四项
ROW_PRIORITY = ("key", "order_id", "source", "station", "ts", "count",
                "false_positive", "rate", "value", "drift_px", "status", "severity")

VALUE_CN = {
    "station": "按工位", "day": "按天", "batch": "按批次", "defect_type": "按缺陷类型",
    "found": "存在", "not_found": "未找到", "pass": "通过", "fail": "未通过",
    "prose": "手册正文", "ok": "成功", "empty": "无结果", "tool_error": "工具报错",
    "high": "高", "medium": "中", "low": "低",
}

UNITS = {
    "maintenance_cycle_days": " 天", "days_since_last": " 天",
    "cumulative_hours": " 小时", "rated_life_hours": " 小时",
    "over_life_hours": " 小时", "remaining_hours": " 小时",
}

_RE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}) 00:00:00$")


def _label(tool: str, key: str) -> str:
    return (FIELD_OVERRIDE.get(tool) or {}).get(key) or FIELD_CN.get(key) or key


def _val(key: str, v) -> str:
    """把值写成现场能读的样子：比率转百分比、小时/天带单位、整点日期去掉 00:00:00。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        if key.endswith("rate") and 0 <= float(v) <= 1:
            return f"{float(v) * 100:.1f}%"
        if key == "score":
            return f"{float(v):.3f}"
        if key in UNITS:
            return f"{int(v)}{UNITS[key]}"
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, str):
        m = _RE_DATE.match(v)
        if m:
            return m.group(1)
        if v in VALUE_CN:
            return VALUE_CN[v]
        # group_by 常是逗号拼接的多维度：station,defect_type,batch
        # 整体查表查不到，就逐项翻再拼回去，别把三个英文枚举甩给现场
        if "," in v:
            parts = [VALUE_CN.get(x.strip(), x.strip()) for x in v.split(",")]
            if any(p in VALUE_CN.values() for p in parts):
                return "、".join(parts)
        if key in FULL_TEXT_KEYS:
            return v
        return v[:70] + "…" if len(v) > 70 else v
    return str(v)


# 结论正文里的溯源标签：[tool{args}] 或 [tool]。这是模型写出来的原文，
# 里面是英文工具名和 group_by=station 这种枚举，现场一个字都读不出意思。
_CITE_RE = re.compile(r"\[([a-z][a-z0-9_]*)(?:\{([^}]*)\})?\]")


def _cite_args(tool: str, inner: str) -> str:
    """把 `group_by=station,defect_type,batch` 还原成"分组维度=按工位、按缺陷类型、按批次"。

    逗号在两处都用：既分隔参数，也分隔多值枚举。所以先按逗号切，
    含 "=" 的段当新参数，不含的并回上一个参数的值——不能简单 split("=")。
    """
    pairs: list[list] = []
    for seg in inner.split(","):
        seg = seg.strip()
        if "=" in seg:
            k, v = seg.split("=", 1)
            pairs.append([k.strip(), [v.strip()]])
        elif pairs:
            pairs[-1][1].append(seg)
    out = []
    for k, vs in pairs:
        out.append(f"{_label(tool, k)}={'、'.join(_val(k, v) for v in vs)}")
    return "，".join(out)


def cite_text(text):
    """只翻认得的工具名，认不出的一字不改——正文是证据，宁可不翻也不能翻错。"""
    if not text:
        return text

    def sub(m):
        name, inner = m.group(1), m.group(2)
        cn = tool_cn(name)
        if cn == name:
            return m.group(0)
        return f"[{cn}]" if not inner else f"[{cn}{{{_cite_args(name, inner)}}}]"

    return _CITE_RE.sub(sub, text)


def _row_text(tool: str, row) -> str:
    """列表里的一条压成一行：按优先级挑字段，跳过长正文，最多四对。"""
    if not isinstance(row, dict):
        return _val("", row)
    keys = []
    for k in ROW_PRIORITY:
        if k in row and not isinstance(row[k], (list, tuple, dict)):
            keys.append(k)
    for k in row:
        if k in keys or k in ("text", "by_param"):
            continue
        if isinstance(row[k], (list, tuple, dict)):
            continue
        keys.append(k)
    parts = [f"{_label(tool, k)} {_val(k, row[k])}" for k in keys[:4]]
    return " · ".join(parts)


def _view(tool: str, res) -> dict:
    """一条工具返回的中文视图：字段表 + 主要列表的少量样例。

    只挑标量字段，列表单独成块——把 200 条记录铺到页面上没有意义，
    现场要的是"有几个、长什么样"。
    """
    res = _as_obj(res)
    if isinstance(res, str):
        return {"fields": [], "list": None, "note": res[:80]}
    if isinstance(res, list):
        return {"fields": [], "list": {"label": "记录", "count": len(res),
                                        "items": [_row_text(tool, r) for r in res[:3]]}}
    if not isinstance(res, dict):
        return {"fields": [], "list": None, "note": str(res)[:80]}

    fields = []
    lists = []
    for k, v in res.items():
        if isinstance(v, (list, tuple)):
            lists.append((k, v))
        elif isinstance(v, dict):
            continue
        elif len(fields) < 12:
            fields.append({"label": _label(tool, k), "value": _val(k, v)})

    out = {"fields": fields, "list": None}
    if lists:
        pref = PREFERRED_LIST.get(tool)
        picked = next((kv for kv in lists if kv[0] == pref), None) if pref else None
        if picked is None:
            picked = max(lists, key=lambda kv: len(kv[1]))
        k, v = picked
        out["list"] = {"label": _label(tool, k), "count": len(v),
                       "items": [_row_text(tool, r) for r in v[:3]]}
    return out


def _brief(evidence, steps=0):
    """给前端用的证据摘要：只留判定需要的字段，不把全量返回塞进页面。

    多轮会话里 evidence 是累积的：第二轮可能只查 1 次，evidence 却有 11 条。
    界面如果只显示"工具调用 1 次"却给出 10 个数字，用户会误判这些数字是刚查的。
    所以按 steps 切出本轮新增的那几条，给每条打 current 标记，前端据此分层展示。
    """
    out = []
    cur = evidence[-steps:] if steps > 0 else []
    cur_ids = {id(x) for x in cur}
    for e in evidence:
        res = _as_obj(e.get("result"))
        ok = not (isinstance(res, dict) and "error" in res)
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        out.append({
            "tool": e["tool"],
            "tool_cn": tool_cn(e["tool"]),
            "args": e["args"],
            # 参数也翻译：group_by=station 这种，现场看不出是"按工位分组"
            "args_view": [{"label": _label(e["tool"], k), "value": _val(k, v)}
                          for k, v in args.items()],
            "ok": ok,
            "error": (res.get("error") if not ok else None),
            "summary": _summarize(res),
            # 中文字段表：拿完整对象算，前端只负责排版（head 是截断串，parse 靠不住）
            "view": _view(e["tool"], res),
            "head": json.dumps(res, ensure_ascii=False, default=str)[:600],
            "current": id(e) in cur_ids,
        })
    return out


