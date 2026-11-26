"""延迟分布测试：区分"排队"和"生成本身"。

背景：用户反馈中转站慢是排队导致，当前排队约 5 秒。
如果属实，那么一次诊断 6-12 步就要付 6-12 次排队，排队成本按步数线性累加。
本脚本连发 N 次同参请求，看耗时是否集中在一个固定的"地基"上——那就是排队。

判读方式：
- 若每次耗时 = 固定偏移(约5s) + 小抖动  -> 纯排队，优化方向是减步数
- 若耗时忽大忽小(3/54/11)              -> 队列不稳定，优化方向是错峰/重试
"""

import os
import statistics
import time
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
    base = os.environ.get("FDE_BASE_URL", "https://ai-api.minekuai.cn/v1")
    model = os.environ.get("FDE_MODEL", "deepseek-v4.1-flash-1017")
    key = os.environ.get("FDE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    c = OpenAI(api_key=key, base_url=base, timeout=120, max_retries=0)

    # 极小请求，排除"生成长度"的干扰，看到的耗时基本就是纯排队 + 网络
    msgs = [{"role": "user", "content": "只回复一个字符：ok"}]

    print(f"model={model}  连发 6 次极小请求（max_tokens=4），间隔 0s\n")
    ts = []
    for i in range(6):
        t0 = time.perf_counter()
        try:
            r = c.chat.completions.create(model=model, messages=msgs,
                                          temperature=0, max_tokens=4)
            dt = time.perf_counter() - t0
            ts.append(dt)
            print(f"  第{i+1}次 {dt:6.2f}s  usage={r.usage.prompt_tokens}/"
                  f"{r.usage.completion_tokens}")
        except Exception as e:
            dt = time.perf_counter() - t0
            print(f"  第{i+1}次 {dt:6.2f}s  失败 {type(e).__name__} {str(e)[:60]}")

    if ts:
        print(f"\n最小 {min(ts):.2f}s  中位 {statistics.median(ts):.2f}s  最大 {max(ts):.2f}s")
        print(f"抖动 = 最大-最小 = {max(ts)-min(ts):.2f}s")
        print("\n按中位数推算一条 12 步诊断的纯 LLM 时间上限："
              f"{statistics.median(ts)*12:.0f}s（不含工具执行与本地开销）")


if __name__ == "__main__":
    main()
