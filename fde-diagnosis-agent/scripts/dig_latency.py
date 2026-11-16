"""从轨迹拆时间账：133s 的耗时到底花在哪。

轨迹每行有 t 字段（相对起始的毫秒）。相邻事件的时间差就是该阶段的真实开销。
把 plan -> tool 归为"LLM 生成"，tool 内部算"工具执行"，其余归为本地开销。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"


def analyse(path: Path):
    recs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not recs:
        return
    start = recs[0].get("user_msg", "")[:40]
    total_ms = recs[-1]["t"]

    # 归因规则（很容易搞反，这里写死）：
    # 事件是在动作"完成之后"才写入的。所以相邻两事件的 gap 属于"后一个事件"。
    #   上一事件 understand -> 当前 plan      gap = plan 这次 LLM 生成
    #   上一事件 plan       -> 当前 tool      gap = 该工具执行
    # 反过来归因会把 92% 的 LLM 时间算成工具时间，优化方向会整个打偏。
    buckets = {"LLM生成": 0, "工具执行": 0, "本地/其他": 0}
    prev = recs[0]
    plan_n = tool_n = 0
    llm_detail = {}
    tool_detail = {}

    for rec in recs[1:]:
        gap = rec["t"] - prev["t"]
        ev = rec["event"]
        if ev in ("plan", "understand", "knowledge", "plan_retry"):
            buckets["LLM生成"] += gap
            plan_n += 1
            llm_detail[ev] = llm_detail.get(ev, 0) + gap
        elif ev == "tool":
            buckets["工具执行"] += gap
            tool_n += 1
            name = rec.get("name") or rec.get("tool") or "?"
            tool_detail[name] = tool_detail.get(name, 0) + gap
        else:
            buckets["本地/其他"] += gap
        prev = rec

    print(f"文件: {path.name}")
    print(f"输入: {start}")
    print(f"总耗时: {total_ms/1000:.1f}s   LLM调用={plan_n}  工具调用={tool_n}")
    print("-" * 62)
    for k, v in buckets.items():
        pct = v / total_ms * 100 if total_ms else 0
        print(f"  {k:10s} {v/1000:7.1f}s  {pct:5.1f}%")
    if plan_n:
        print(f"\n  单次 LLM 平均: {buckets['LLM生成']/1000/plan_n:.1f}s")
        for k, ms in sorted(llm_detail.items(), key=lambda x: -x[1]):
            print(f"    {k:12s} {ms/1000:6.1f}s")
    if tool_n:
        print(f"  单次工具平均: {buckets['工具执行']/1000/tool_n:.1f}s")
        for name, ms in sorted(tool_detail.items(), key=lambda x: -x[1]):
            print(f"    {name:26s} {ms/1000:6.1f}s")


def main():
    if len(sys.argv) > 1:
        targets = [RUNS / sys.argv[1]]
    else:
        # 默认取最近 3 个有 result 的
        files = sorted(RUNS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        targets = files[:3]
    for t in targets:
        analyse(t)
        print("=" * 62)


if __name__ == "__main__":
    main()
