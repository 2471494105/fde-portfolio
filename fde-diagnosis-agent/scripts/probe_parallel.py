"""并发可行性测试：慢是因为排队，那并发就等于"同时排多个队"。

串行 24 条要 50+ 分钟，如果排队能并行，总时间能压到几分钟。
风险是限流：串行 12/12 没问题，并发可能触发 429。

本脚本用 3 / 6 两档并发，各发 12 个真实尺寸的请求，看失败率与总耗时。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
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
    base = os.environ.get("FDE_BASE_URL")
    model = os.environ.get("FDE_MODEL")
    key = os.environ.get("FDE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    client = OpenAI(api_key=key, base_url=base, timeout=120, max_retries=1)

    def one(i):
        t0 = time.perf_counter()
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": f"只回复一个字：{i}"}],
                temperature=0, max_tokens=4,
            )
            return True, time.perf_counter() - t0
        except Exception as e:
            return False, str(e)[:60]

    for workers in (3, 6):
        print(f"\n并发 {workers}，共 12 个请求")
        t_all = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            res = list(pool.map(one, range(12)))
        total = time.perf_counter() - t_all
        ok = [r for r in res if r[0]]
        bad = [r for r in res if not r[0]]
        print(f"  总耗时 {total:.1f}s  成功 {len(ok)}/12  失败 {len(bad)}/12")
        if ok:
            print(f"  单请求耗时 min={min(r[1] for r in ok):.1f}s "
                  f"max={max(r[1] for r in ok):.1f}s")
        for b in bad[:3]:
            print(f"  失败样本: {b[1]}")
        # 串行推算值用于对比
        if ok:
            serial_est = sum(r[1] for r in ok) / len(ok) * 12
            print(f"  同批串行推算约 {serial_est:.0f}s，加速 {serial_est/total:.1f}x")


if __name__ == "__main__":
    main()
