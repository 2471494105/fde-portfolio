"""校验层：参数前置校验 + 结论溯源校验 + 修正重试提示。

设计原则：不靠模型自觉，靠程序强制。
这一层与模型无关，换任何模型都能复用 —— 这也是"可审计"能力的落点。
"""

import json
import re

from seed import DEVICES, LOGS

DATA_START = min(r["ts"] for r in LOGS)[:10]
DATA_END = max(r["ts"] for r in LOGS)[:10]

# 需要设备号的工具
DEVICE_TOOLS = {"query_device_profile", "query_defect_log", "query_light_usage"}


def validate_args(tool_name: str, args: dict, user_msg: str = ""):
    """工具执行前的参数校验。返回 (ok, error_payload)。"""
    did = (args or {}).get("device_id")

    if tool_name in DEVICE_TOOLS and did:
        # 归属校验：用户没提过任何设备号，就不许自己选一台去查
        mentioned = [d for d in DEVICES if d in user_msg]
        if not mentioned:
            return False, {
                "error": "用户没有提供设备编号，不能自行指定设备",
                "hint": f"现有设备：{', '.join(DEVICES)}。请先向用户确认是哪一台，不要猜测。",
            }
        # 存在性校验
        if did not in DEVICES:
            return False, {
                "error": f"设备 {did} 不在设备清单中",
                "hint": f"现有设备：{', '.join(DEVICES)}。如果用户没有提供设备编号，不要猜测，请先向用户确认是哪一台。",
            }

    if tool_name == "query_defect_log":
        for key in ("time_from", "time_to"):
            val = (args or {}).get(key) or ""
            if val and (val[:10] < DATA_START or val[:10] > DATA_END):
                return False, {
                    "error": f"{key}={val} 超出数据覆盖范围",
                    "hint": f"日志仅覆盖 {DATA_START} 至 {DATA_END}。请调整时间窗，或留空表示查全部。",
                }

    return True, None


def _num_sourced(num_str: str, blob: str) -> bool:
    """判断结论里的一个数字能否在工具返回中找到（含百分比换算）。"""
    if num_str in blob:
        return True
    raw = num_str.rstrip("%")
    if raw and raw in blob:
        return True
    if num_str.endswith("%"):
        try:
            cand = float(raw) / 100
        except ValueError:
            return False
        for fmt in (f"{cand:.3f}", f"{cand:.2f}", f"{cand:.4f}", str(round(cand, 3))):
            if fmt in blob:
                return True
    return False


def check_traceability(final: str, tool_results: list, user_msg: str = "") -> list:
    """结论里出现的百分比、设备号，必须能在工具返回里找到。返回无据清单。

    设备号额外放宽：用户自己输入过的设备号不算编造。
    """
    blob = json.dumps(tool_results, ensure_ascii=False)
    unsourced = []
    for num in set(re.findall(r"\d+(?:\.\d+)?%", final or "")):
        if not _num_sourced(num, blob):
            unsourced.append(num)
    for dev in set(re.findall(r"CX-\d+", final or "")):
        if dev not in blob and dev not in (user_msg or ""):
            unsourced.append(dev)
    return sorted(unsourced)


def build_repair_prompt(unsourced: list, extra_hint: str = "") -> str:
    items = "、".join(unsourced)
    msg = (
        f"你刚才的结论里出现了工具结果中查不到的内容：{items}。\n"
        "请重写结论，规则如下：\n"
        "1. 只能引用工具实际返回过的数字，不得自行换算、推算或估计；\n"
        "2. 工具没返回的数字，一律不要写；\n"
        "3. 如果证据不足以支撑结论，就直接说明证据不足、需要进一步核对。\n"
        "只输出重写后的结论，不要解释你在做什么。"
    )
    if extra_hint:
        msg += f"\n补充：{extra_hint}"
    return msg
