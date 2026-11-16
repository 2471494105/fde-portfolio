"""统计各模型的 token 与耗时，用来算"换本地到底省了什么、代价是什么"。

数据来源：runs/*.jsonl 的 run_start（记了 model）与 run_end（记了 tokens_total / elapsed_ms）。
"""

import io
import json
from collections import defaultdict
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"


def main():
    stats = defaultdict(lambda: {"n": 0, "tokens": 0, "ms": 0, "steps": 0})
    for p in sorted(RUNS.glob("*.jsonl")):
        try:
            ev = [json.loads(l) for l in io.open(p, encoding="utf-8") if l.strip()]
        except Exception:
            continue
        start = next((e for e in ev if e["event"] == "run_start"), {})
        end = next((e for e in ev if e["event"] == "run_end"), {})
        model = start.get("model") or "unknown"
        s = stats[model]
        s["n"] += 1
        s["tokens"] += end.get("tokens_total", 0) or 0
        s["ms"] += end.get("elapsed_ms", 0) or 0
        s["steps"] += (next((e for e in ev if e["event"] == "result"), {}) or {}).get("steps", 0) or 0

    print(f"{'模型':<18}{'条数':>6}{'合计token':>12}{'单条token':>11}{'单条耗时':>11}{'单条步数':>10}")
    for model, s in sorted(stats.items(), key=lambda x: -x[1]["n"]):
        n = s["n"]
        print(f"{model:<18}{n:>6}{s['tokens']:>12}"
              f"{round(s['tokens']/n):>11}{s['ms']/n/1000:>10.1f}s{s['steps']/n:>10.2f}")


if __name__ == "__main__":
    main()
