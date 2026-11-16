"""W0 跑分：验证 qwen2.5:7b 在 3 工具场景下的可用性。

用法：
    python run_w0.py --rounds 1          # 先跑一遍看链路通不通
    python run_w0.py --rounds 4          # 全量 20 次（5 条 × 4 轮）
    python run_w0.py --rounds 4 --traj H1
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from agent_loop import run_agent
from guard import check_traceability
from llm import get_client
from seed import DEVICES
from trajectories import TRAJECTORIES

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def is_subseq(sub, seq):
    """sub 是否为 seq 的子序列（按顺序出现即可）。"""
    it = iter(seq)
    return all(any(x == s for x in it) for s in sub)


def evaluate(traj, result):
    calls = [t["tool"] for t in result["trace"]]
    final = result["final"] or ""

    # 编造检查：用户没提到的设备编号，模型自己填了
    mentioned = [d for d in DEVICES if d in traj["input"]]
    fabricated = sorted({
        t["args"].get("device_id")
        for t in result["trace"]
        if (t["args"] or {}).get("device_id") and t["args"].get("device_id") not in mentioned
    })

    # 路径
    if traj["allowed_paths"] is None:
        path_ok = True
    else:
        path_ok = any(is_subseq(p, calls) for p in traj["allowed_paths"])

    # 参数（期望维度是否在取证过程中被用过；不要求第一次就切对，
    # 先按 hour 看趋势、再按 station 定位是更合理的诊断顺序）
    arg_ok = True
    if traj["expect_group_by"]:
        group_bys = [t["args"].get("group_by", "") for t in result["trace"] if t["tool"] == "query_defect_log"]
        arg_ok = any(g == traj["expect_group_by"] for g in group_bys)

    # 终局
    final_ok = True
    if traj["final_keywords"]:
        final_ok = any(k in final for k in traj["final_keywords"])

    # 忌讳行为（过度追问等）
    forbid_hit = False
    if traj["forbid_keywords"]:
        forbid_hit = any(k in final for k in traj["forbid_keywords"])

    # 证据溯源（复用 guard 的实现，保持单一真源）
    unsourced = check_traceability(final, [t.get("result") for t in result["trace"]], traj["input"])
    evidence_ok = not unsourced

    # 被护栏拦住的编造尝试不判交付失败：它没有真正执行，交付物是最终输出。
    # 只有"真的执行了越权查询"才算失败。
    fab_executed = sorted({
        t["args"].get("device_id")
        for t in result["trace"]
        if (t["args"] or {}).get("device_id")
        and t["args"].get("device_id") not in mentioned
        and not t.get("blocked")
    })
    if fab_executed:
        final_ok = False

    return dict(
        path_ok=path_ok,
        arg_ok=arg_ok,
        final_ok=final_ok and not forbid_hit,
        forbid_hit=forbid_hit,
        fabricated=fabricated,
        evidence_ok=evidence_ok,
        unsourced=unsourced,
        calls=calls,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--model", default="")
    parser.add_argument("--backend", default="ollama", choices=["ollama", "deepseek"])
    parser.add_argument("--traj", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    client = get_client(args.backend, args.model)
    trajs = [t for t in TRAJECTORIES if not args.traj or t["id"] == args.traj]
    rows = []
    started = datetime.now()

    for traj in trajs:
        for r in range(args.rounds):
            print(f"[{traj['id']}] 第 {r + 1}/{args.rounds} 轮 ...", flush=True)
            result = run_agent(traj["input"], client=client, verbose=args.verbose)
            ev = evaluate(traj, result)
            rows.append(dict(traj=traj, result=result, ev=ev, round=r + 1))
            ok_desc = "/".join([
                "路径✓" if ev["path_ok"] else "路径✗",
                "参数✓" if ev["arg_ok"] else "参数✗",
                "终局✓" if ev["final_ok"] else "终局✗",
            ])
            fab = f"  编造={ev['fabricated']}" if ev["fabricated"] else ""
            src = f"  无据={ev['unsourced']}" if ev["unsourced"] else ""
            guard = f"  拦截={result.get('blocked', 0)}" if result.get("blocked") else ""
            print(f"    调用：{ev['calls']}  {ok_desc}  fc失败={result['fc_fail']}{fab}{src}{guard}"
                  + (f"  err={result['error']}" if result["error"] else ""), flush=True)

    # 汇总
    n = len(rows)
    path_rate = sum(1 for x in rows if x["ev"]["path_ok"]) / n
    arg_rate = sum(1 for x in rows if x["ev"]["arg_ok"]) / n
    final_rate = sum(1 for x in rows if x["ev"]["final_ok"]) / n
    fc_total = sum(x["result"]["fc_fail"] for x in rows)
    call_total = sum(len(x["result"]["trace"]) for x in rows)
    avg_steps = sum(x["result"]["steps"] for x in rows) / n
    forbid_total = sum(1 for x in rows if x["ev"]["forbid_hit"])
    fab_total = sum(1 for x in rows if x["ev"]["fabricated"])
    semantic_ok = [
        x for x in rows
        if x["ev"]["path_ok"] and x["ev"]["arg_ok"] and x["ev"]["final_ok"]
        and x["ev"]["evidence_ok"]
    ]
    semantic_rate = len(semantic_ok) / n
    blocked_total = sum(x["result"].get("blocked", 0) for x in rows)
    repair_total = sum(x["result"].get("repairs", 0) for x in rows)

    print("\n" + "=" * 56)
    print(f"模型：{client.name}   样本：{n} 次   耗时：{(datetime.now() - started).seconds}s")
    print(f"路径命中率 : {path_rate:.1%}")
    print(f"参数命中率 : {arg_rate:.1%}")
    print(f"终局命中率 : {final_rate:.1%}")
    print(f"过度追问   : {forbid_total} 次")
    print(f"编造设备号 : {fab_total} 次")
    print(f"语义正确率 : {semantic_rate:.1%}   ← 路径+参数+终局+溯源 全通过")
    print(f"护栏拦截   : {blocked_total} 次（参数越界/设备不存在）")
    print(f"修正重试   : {repair_total} 次")
    print(f"fc 解析失败: {fc_total} / {call_total} 次调用")
    print(f"平均步数   : {avg_steps:.1f}")
    print("=" * 56)

    if semantic_rate >= 0.9:
        if fab_total:
            verdict = (f"交付正确率 {semantic_rate:.0%}；"
                       f"其中 {fab_total} 次是拦住编造后转追问（护栏是必需的）")
        else:
            verdict = f"交付正确率 {semantic_rate:.0%}，可以推进"
    elif semantic_rate >= 0.6:
        verdict = "勉强可用，必须保留参数校验与证据溯源，或换模型对比"
    else:
        verdict = "语义不可靠，换模型或加强护栏"
    print(f"结论：{verdict}")

    # 落盘报告
    suffix = "_deepseek" if args.backend == "deepseek" else ""
    out = Path(__file__).with_name(f"w0_report{suffix}.md")
    lines = [
        "# W0 可行性验证报告", "",
        f"- 模型：`{client.name}`",
        f"- 样本：{n} 次",
        f"- 时间：{started:%Y-%m-%d %H:%M}",
        "",
        "## 汇总", "",
        "| 指标 | 结果 |", "|---|---|",
        f"| 路径命中率 | {path_rate:.1%} |",
        f"| 参数命中率 | {arg_rate:.1%} |",
        f"| 终局命中率 | {final_rate:.1%} |",
        f"| 过度追问 | {forbid_total} 次 |",
        f"| 编造设备号 | {fab_total} 次 |",
        f"| 语义正确率 | {semantic_rate:.1%} |",
        f"| 护栏拦截 | {blocked_total} 次 |",
        f"| 修正重试 | {repair_total} 次 |",
        f"| fc 解析失败 | {fc_total} / {call_total} |",
        f"| 平均步数 | {avg_steps:.1f} |",
        "",
        f"**结论：{verdict}**",
        "",
        "## 明细", "",
        "| 轨迹 | 轮 | 实际调用 | 路径 | 参数 | 终局 | 溯源 | 编造 | fc失败 | 回复摘要 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for x in rows:
        ev, res = x["ev"], x["result"]
        summary = (res["final"] or "").replace("\n", " ")[:60]
        fab = ",".join(ev["fabricated"]) or "-"
        lines.append(
            f"| {x['traj']['id']} | {x['round']} | {' → '.join(ev['calls']) or '（无调用）'} | "
            f"{'✓' if ev['path_ok'] else '✗'} | {'✓' if ev['arg_ok'] else '✗'} | "
            f"{'✓' if ev['final_ok'] else '✗'} | "
            f"{'✓' if ev['evidence_ok'] else '✗(' + ','.join(ev['unsourced']) + ')'} | "
            f"{fab} | {res['fc_fail']} | {summary} |"
        )
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入：{out}")


if __name__ == "__main__":
    main()
