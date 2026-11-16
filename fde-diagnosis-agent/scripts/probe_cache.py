"""缓存复测：用真实的 SYSTEM_PROMPT + 7 个工具 schema（约 2100 token）连发三次。

上一轮 probe_compat 用 672 token 的短 prompt，两次都 hit=0。短 prompt 可能不达阈值，
也可能中转站跨请求不共享缓存。这决定"缓存省 64%"这个结论到底站不站得住。
"""

import os
import sys
import time
from pathlib import Path

from openai import OpenAI

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


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
    from app.graph.llm import MODEL, PROVIDER, BASE_URL, get_client
    from app.graph.nodes import SYSTEM_PROMPT
    from app.tools import TOOL_SCHEMAS

    print(f"provider={PROVIDER} model={MODEL} base={BASE_URL}")
    print(f"system prompt 字符数={len(SYSTEM_PROMPT)}  工具数={len(TOOL_SCHEMAS)}")

    c = get_client()
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "[工具预算] 已用 0/12 次。\n\nCX-104 从 11月8号开始误判突然变多"},
    ]

    print("\n连发 3 次完全相同的请求（间隔 0s / 5s）:")
    for i in range(3):
        t0 = time.perf_counter()
        r = c.chat.completions.create(model=MODEL, messages=msgs, tools=TOOL_SCHEMAS, temperature=0)
        u = r.usage
        hit = getattr(u, "prompt_cache_hit_tokens", "无")
        miss = getattr(u, "prompt_cache_miss_tokens", "无")
        pt = getattr(u, "prompt_tokens", 0)
        # 命中率：命中 token 占 prompt 的比例
        ratio = f"{hit/pt*100:.0f}%" if isinstance(hit, int) and pt else "n/a"
        print(f"  第{i+1}次 prompt={pt} hit={hit} miss={miss} 命中率={ratio} "
              f"耗时={time.perf_counter()-t0:.1f}s")
        time.sleep(5)

    print("\n判读：")
    print("  若第2/3次 hit 仍为 0 —— 中转站跨请求不共享缓存，之前 82.7% 只发生在同一个 run 内，")
    print("  即 plan 2 复用了 plan 1 的前缀。那仍然有用（一条诊断 5-8 步），但别指望跨 case 省钱。")


if __name__ == "__main__":
    main()
