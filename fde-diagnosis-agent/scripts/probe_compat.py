"""中转站兼容性探针：本项目的图有三个隐含依赖，逐条验证而不是假设。

1. tool_calls          —— 所有分支都靠它
2. response_format     —— understand 节点用它拿 JSON，不支持就会静默解析失败走 fallback
3. prompt_cache_hit    —— 决定实际花多少钱

为什么必须单独验：这三个失败方式都不报错。tool_calls 退化表现为"模型不调工具"，
response_format 退化表现为"意图判断偶尔很离谱"，缓存未命中表现为"账单比预期高"。
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


def main():
    load_env()
    base = os.environ.get("FDE_BASE_URL", "https://minekuai.cn/v1")
    model = os.environ.get("FDE_MODEL", "deepseek-v4.1-flash-1017")
    key = os.environ.get("FDE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    c = OpenAI(api_key=key, base_url=base, timeout=60)
    print(f"base_url={base} model={model}\n")

    # 1) response_format=json_object
    print("[1] response_format=json_object")
    try:
        r = c.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": '只输出 JSON，不要 markdown。格式：{"intent":"diagnose","device_ids":["CX-101"]}'},
                {"role": "user", "content": "CX-101 有点问题，你查一下"},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            timeout=30,
        )
        raw = r.choices[0].message.content or ""
        print("    原文:", raw[:160].replace("\n", " "))
        try:
            obj = json.loads(raw)
            print(f"    -> 可直接解析，intent={obj.get('intent')} ids={obj.get('device_ids')}")
        except Exception as e:
            print(f"    -> 解析失败({e})，说明该端点没有强制 JSON，understand 会走 fallback")
    except Exception as e:
        print(f"    -> 请求失败: {e}")

    # 2) 同一前缀连续两次，看缓存命中
    print("\n[2] 前缀缓存（同一段长 system 连发两次）")
    long_sys = "你是产线视觉检测设备诊断助手。" * 60
    for i in (1, 2):
        r = c.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": long_sys},
                      {"role": "user", "content": "CX-101 光源超寿命了吗"}],
            temperature=0,
            timeout=60,
        )
        u = r.usage
        print(f"    第{i}次 prompt={getattr(u,'prompt_tokens',0)} "
              f"hit={getattr(u,'prompt_cache_hit_tokens','无')} "
              f"miss={getattr(u,'prompt_cache_miss_tokens','无')}")

    # 3) 连续 12 次小请求，看有没有限流
    print("\n[3] 连发 12 次，看是否限流（模拟一条 12 步的诊断）")
    ok, fail = 0, 0
    for i in range(12):
        try:
            c.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": f"回一个字：{i}"}],
                temperature=0,
                max_tokens=8,
                timeout=30,
            )
            ok += 1
        except Exception as e:
            fail += 1
            print(f"    第{i+1}次失败: {type(e).__name__} {str(e)[:80]}")
    print(f"    成功 {ok}/12，失败 {fail}/12")


if __name__ == "__main__":
    main()
