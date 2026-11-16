"""原生 tool-calling 循环 + 校验层（后端无关）。

刻意不用 LangGraph —— 要测的是模型本身的 function calling 能力。
校验层与模型解耦：参数前置校验 → 结论溯源校验 → 修正重试 → 残留标注。
"""

import json
import sys

from guard import build_repair_prompt, check_traceability, validate_args
from llm import get_client
from tools import TOOL_SCHEMAS, TOOL_IMPLS

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SYSTEM_PROMPT = """你是工厂视觉检测设备的故障诊断助手。设备由相机、镜头、光源、传送带组成，
用于检测产品外观（划痕、凹坑、尺寸、污渍）。

工作方式：
1. 先取证，再下结论。能用工具查到的，不要靠猜。
2. 查到证据后再判断方向：误判集中在某个工位，多半是那个工位的硬件问题（光源衰减、镜头污染、
   标定漂移）；误判随机分布在各工位、或集中在某个物料批次，多半是模型或阈值问题。
3. 信息不足时，先尝试用工具补（例如没给时间就查最近记录）；确实拿不到关键信息时，
   再向操作员追问。
4. 工具报错或查不到数据时，如实说明，不要编造。

最终回答用中文，结构：结论 / 依据 / 建议动作。"""


def run_agent(user_msg: str, client=None, max_steps: int = 6,
              verbose: bool = False, use_guard: bool = True) -> dict:
    if client is None:
        client = get_client()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    trace = []
    fc_fail = 0
    blocked = 0
    consecutive_blocks = 0
    final = "（达到最大步数仍未给出结论）"
    steps = 0
    error = ""

    for step in range(max_steps):
        steps = step
        try:
            out = client.complete(messages, TOOL_SCHEMAS)
        except Exception as exc:
            return {
                "final": "", "trace": trace, "fc_fail": fc_fail, "steps": steps,
                "error": f"model_error: {exc}", "blocked": blocked,
                "repairs": 0, "unsourced": [],
            }

        calls = out.get("tool_calls") or []
        if not calls:
            final = (out.get("content") or "").strip()
            break

        messages.append({
            "role": "assistant",
            "content": out.get("content") or "",
            "tool_calls": calls,
        })

        for idx, call in enumerate(calls):
            name = call["name"]
            args = call["arguments"] if isinstance(call.get("arguments"), dict) else {}
            call_id = call.get("id") or f"call_{step}_{idx}"

            blocked_flag = False
            result = None

            if use_guard:
                ok, err = validate_args(name, args, user_msg)
                if not ok:
                    result = err
                    blocked += 1
                    consecutive_blocks += 1
                    blocked_flag = True

            if not blocked_flag:
                fn = TOOL_IMPLS.get(name)
                if fn is None:
                    result = {"error": f"未知工具 {name}"}
                    fc_fail += 1
                else:
                    try:
                        result = fn(**args)
                    except TypeError as exc:
                        result = {"error": f"参数不合法：{exc}"}
                        fc_fail += 1
                    except Exception as exc:
                        result = {"error": str(exc)}

            trace.append({
                "step": step,
                "tool": name,
                "args": args,
                "ok": "error" not in result,
                "blocked": blocked_flag,
                "result": result,
            })
            if verbose:
                flag = "拦截" if blocked_flag else ("ok" if "error" not in result else "ERR")
                print(f"  [{step}] {name} {json.dumps(args, ensure_ascii=False)} -> {flag}")

            messages.append({
                "role": "tool",
                "name": name,
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            })

            if not blocked_flag:
                consecutive_blocks = 0

        # 熔断：连续两次被拦截，说明模型在原地打转，直接停下来问用户
        if consecutive_blocks >= 2:
            final = ("需要先确认设备信息：请问是哪一台？请提供设备编号。"
                     "不确认清楚我不做诊断，避免猜错设备。")
            error = "guard_circuit_breaker"
            break

    # ---- 结论溯源校验 + 修正重试 ----
    tool_results = [t["result"] for t in trace]
    unsourced = check_traceability(final, tool_results, user_msg) if use_guard else []
    repairs = 0

    if unsourced:
        messages.append({"role": "assistant", "content": final})
        messages.append({"role": "user", "content": build_repair_prompt(unsourced)})
        try:
            out2 = client.complete(messages, None)
            new_final = (out2.get("content") or "").strip()
            repairs = 1
            new_unsourced = check_traceability(new_final, tool_results, user_msg)
            if new_final and len(new_unsourced) <= len(unsourced):
                final, unsourced = new_final, new_unsourced
        except Exception:
            pass

    if unsourced:
        final += f"\n\n[未核实数字：{', '.join(unsourced)}]"

    return {
        "final": final,
        "trace": trace,
        "fc_fail": fc_fail,
        "steps": steps,
        "error": error,
        "blocked": blocked,
        "repairs": repairs,
        "unsourced": unsourced,
    }
