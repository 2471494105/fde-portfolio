"""W0 三个工具：设备档案、误判日志聚合、光源使用。"""

from collections import defaultdict
from typing import Literal

from seed import DEVICES, LIGHT_USAGE, LOGS


def query_device_profile(device_id: str) -> dict:
    dev = DEVICES.get(device_id)
    if not dev:
        return {"error": f"设备 {device_id} 不存在，现有设备：{list(DEVICES)}"}
    return dev


def query_defect_log(
    device_id: str,
    time_from: str = "",
    time_to: str = "",
    group_by: Literal["hour", "station", "defect_type", "batch"] = "hour",
) -> dict:
    rows = [r for r in LOGS if r["device_id"] == device_id]
    if time_from:
        rows = [r for r in rows if r["ts"] >= time_from]
    if time_to:
        rows = [r for r in rows if r["ts"] <= time_to]
    if not rows:
        return {"error": f"设备 {device_id} 在指定时间窗内无记录"}

    key_map = {"hour": "ts", "station": "station", "defect_type": "defect_type", "batch": "batch"}
    key = key_map.get(group_by, "ts")

    buckets = defaultdict(lambda: {"total": 0, "false_positive": 0})
    for r in rows:
        k = r[key]
        if group_by == "hour":
            k = r["ts"][:13]  # 按小时
        buckets[k]["total"] += 1
        if r["is_actually_ok"]:
            buckets[k]["false_positive"] += 1

    total = len(rows)
    fp = sum(1 for r in rows if r["is_actually_ok"])
    groups = [
        {"key": k, "count": v["total"], "false_positive": v["false_positive"],
         "rate": round(v["false_positive"] / v["total"], 3)}
        for k, v in sorted(buckets.items())
    ]

    night_fp = sum(1 for r in rows if r["is_actually_ok"] and r["night_shift"])
    samples = rows[:3]

    return {
        "device_id": device_id,
        "total": total,
        "false_positive": fp,
        "false_positive_rate": round(fp / total, 3),
        "group_by": group_by,
        "groups": groups,
        "night_shift_false_positive": night_fp,
        "samples": samples,
    }


def query_light_usage(device_id: str) -> dict:
    usage = LIGHT_USAGE.get(device_id)
    if not usage:
        return {"error": f"设备 {device_id} 无光源记录"}
    return dict(device_id=device_id, **usage)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_device_profile",
            "description": "查询视觉检测设备的档案：产线、工位、相机型号、镜头型号、光源型号、装机日期。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号，例如 CX-101"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_defect_log",
            "description": (
                "查询设备的误判/漏判日志并按维度聚合。group_by 可选："
                "hour（按小时看时间趋势）、station（按工位看是否集中在某工位）、"
                "defect_type（按缺陷类型看是否某类缺陷被误判）、batch（按物料批次看是否换料导致）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号"},
                    "time_from": {"type": "string", "description": "开始时间，格式 YYYY-MM-DD HH:MM:SS，可留空"},
                    "time_to": {"type": "string", "description": "结束时间，可留空"},
                    "group_by": {
                        "type": "string",
                        "enum": ["hour", "station", "defect_type", "batch"],
                        "description": "聚合维度，默认 hour",
                    },
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_light_usage",
            "description": "查询光源累计点亮时长、额定寿命、当前亮度设定、亮度调整历史、更换记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号"},
                },
                "required": ["device_id"],
            },
        },
    },
]

TOOL_IMPLS = {
    "query_device_profile": query_device_profile,
    "query_defect_log": query_defect_log,
    "query_light_usage": query_light_usage,
}
