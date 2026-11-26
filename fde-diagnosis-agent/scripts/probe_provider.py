"""provider 探针：验证某个 OpenAI 兼容端点是否真的支持 tool calling。

为什么必须单独验：W0 的结论是"tool calling 格式从来不是问题"，那是针对 DeepSeek 官方
和 qwen2.5:7b 的。第三方中转站常在转发时把 tool_calls 降级成普通文本，或者干脆不透传
tools 字段——图里所有分支都依赖 tool_calls，这种站直接不可用，必须在接入前证明。

用法：
    python scripts/probe_provider.py                      # 用 .env 里的配置
    python scripts/probe_provider.py <base_url> <model>   # 临时指定

注意：默认 base_url 原写 https://minekuai.cn/v1，该地址 2026-11-26 返回 410 已退役，
现为 https://ai-api.minekuai.cn/v1。默认值过期会让下次跑的人误以为「官方端点在报错」。
"""

import json
import os
import sys
from pathlib import Path

from openai import OpenAI

_ROOT = Path(__file__).resolve().parents[1]


def load_env():
    for path in (_ROOT / ".env", _ROOT / "w0" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_light_usage",
            "description": "查设备光源累计点亮时长与亮度历史",
            "parameters": {
                "type": "object",
                "properties": {"device_id": {"type": "string", "description": "设备编号，如 CX-101"}},
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_defect_log",
            "description": "查缺陷日志聚合统计",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "group_by": {"type": "string", "enum": ["hour", "day", "station", "defect_type", "batch"]},
                },
                "required": ["device_id"],
            },
        },
    },
]


def main():
    load_env()
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FDE_BASE_URL", "https://ai-api.minekuai.cn/v1")
    model = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("FDE_MODEL", "deepseek-v4.1-flash-1017")
    key = os.environ.get("FDE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("缺少 key")
        return 1

    client = OpenAI(api_key=key, base_url=base, timeout=60)
    print(f"base_url={base}\nmodel={model}")

    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "CX-101 光源是不是超寿命了？顺便按工位看下误判率。"}],
        tools=TOOLS,
        temperature=0,
    )
    m = r.choices[0].message
    print("content:", (m.content or "")[:200])
    print("tool_calls:", [(tc.function.name, tc.function.arguments) for tc in (m.tool_calls or [])])
    u = r.usage
    print(f"usage: prompt={getattr(u, 'prompt_tokens', 0)} completion={getattr(u, 'completion_tokens', 0)}")
    print("缓存字段:", getattr(u, "prompt_cache_hit_tokens", "无"), "/", getattr(u, "prompt_cache_miss_tokens", "无"))

    if not m.tool_calls:
        print("\n结论：该端点没有返回 tool_calls，不能直接接入本项目的图。")
        return 2
    print("\n结论：tool_calls 正常，可接入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
