"""拆一条轨迹的 token 结构：understand 固定成本、每次 plan 的 prompt 增长、工具返回占比。"""

import io
import json
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"


def main(name=None):
    if name:
        p = RUNS / name
    else:
        p = sorted(RUNS.glob("*.jsonl"), key=lambda x: x.stat().st_mtime)[-1]
    print("轨迹:", p.name)
    total = 0
    for line in io.open(p, encoding="utf-8"):
        e = json.loads(line)
        ev = e["event"]
        if ev in ("understand", "plan", "lookup", "knowledge"):
            pt = e.get("prompt_tokens", 0) or 0
            ct = e.get("completion_tokens", 0) or 0
            total += pt + ct
            extra = ""
            if ev == "plan":
                hit = e.get("cache_hit", 0) or 0
                miss = e.get("cache_miss", 0) or 0
                extra = (f" steps_before={e.get('steps_before')} calls={e.get('calls')}"
                         f"  缓存 命中{hit}/未命中{miss}")
            print(f"  {ev:<10} prompt={pt:>5} completion={ct:>4}{extra}")
        elif ev == "tool":
            head = e.get("result_head") or ""
            print(f"  tool {e.get('name'):<22} 返回长度≈{len(head)}（截断后 head）")
        elif ev == "run_end":
            print("  run_end tokens_total=", e.get("tokens_total"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
