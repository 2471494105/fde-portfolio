"""跑 20 条 golden path 并给四类指标打分。

四指标：
- 工具选对率：期望工具出现了几个
- 聚合维度覆盖：期望的 group_by 是否在调用参数里出现过（any 语义，不看顺序）
- 终局正确率：结论关键词命中率
- fc 失败率：工具调用参数解析失败 / 未知工具的次数

语义正确 = 工具命中、维度命中、终局命中、未越禁用工具、步数在区间、无未溯源数字、无 fc 失败。
"""

import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import run  # noqa: E402
from app.graph.llm import BASE_URL, MODEL, PROVIDER  # noqa: E402
from app.meta import code_hash as _code_hash, git_commit as _git_commit  # noqa: E402
from eval.trajectories import TRAJECTORIES  # noqa: E402


def _cases_hash():
    """用例集指纹：改了期望值，旧报告的分数就不再可比。"""
    try:
        blob = json.dumps(TRAJECTORIES, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return ""


def _used_group_by(state):
    cols = []
    for e in state.get("evidence", []):
        gb = e["args"].get("group_by") if isinstance(e["args"], dict) else None
        if gb:
            cols.append(gb)
    return set(cols)


def _norm(text):
    """去掉所有空白（含全角空格）再比较。

    模型输出会出现"1 号工位"（数字与汉字之间带空格）、"1号工位"、
    "1＃工位"等写法差异。按字面比会被一个空格打败，把正确结论判成失败——
    这是测量工具的缺陷，不是模型的问题。
    """
    return re.sub(r"\s+", "", text or "")


def judge(traj, state):
    tools = state.get("tool_names", [])
    final = state.get("final", "") or ""
    steps = state.get("steps", 0)
    blocked = state.get("blocked", [])

    expect = traj["expect_tools"]
    hit = [t for t in expect if t in tools]
    tool_rate = (len(hit) / len(expect)) if expect else 1.0

    forbid_hit = [t for t in traj.get("forbid_tools", []) if t in tools]

    gb_expect = traj.get("expect_group_by", [])
    gb_used = _used_group_by(state)
    gb_hit = [g for g in gb_expect if g in gb_used]
    gb_rate = (len(gb_hit) / len(gb_expect)) if gb_expect else 1.0

    kws = traj.get("final_keywords", [])
    flat = _norm(final)
    kw_hit = [k for k in kws if _norm(k) in flat]
    kw_rate = (len(kw_hit) / len(kws)) if kws else 1.0

    lo, hi = traj["steps_range"]
    steps_ok = lo <= steps <= hi

    fc_fail = any(
        isinstance(e.get("result"), dict) and ("参数 JSON 解析失败" in str(e["result"])
                                               or "未知工具" in str(e["result"]))
        for e in state.get("evidence", [])
    )

    # 幂等验证：直接看建单工具返回的 deduped 字段，比查结论关键词准
    deduped_ok = True
    if traj.get("expect_deduped"):
        orders = [
            e["result"] for e in state.get("evidence", [])
            if isinstance(e.get("result"), dict) and e["result"].get("order_id")
        ]
        deduped_ok = bool(orders) and orders[-1].get("deduped") is True

    # 意图层：考的是"有没有听懂用户在问什么"，不是"诊断得对不对"
    intent_expect = traj.get("expect_intent")
    intent_ok = True
    if intent_expect:
        intent_ok = state.get("intent") == intent_expect

    forbid_words = [w for w in traj.get("forbid_words", []) if _norm(w) in flat]

    # final_required：必须全部出现的关键词，为空则不检查（保持老用例行为不变）
    # 与 final_keywords 的区别：final_keywords 是"同义任选"，阈值 0.5；
    # final_required 是"结论必须落在这些点上"，缺一即判失败。
    req = traj.get("final_required", [])
    req_miss = [w for w in req if _norm(w) not in flat]

    semantic_ok = (
        tool_rate >= 1.0
        and gb_rate >= 1.0
        and kw_rate >= 0.5
        and not forbid_hit
        and steps_ok
        and not blocked
        and not fc_fail
        and deduped_ok
        and intent_ok
        and not forbid_words
        and not req_miss
    )

    # 归因：失败落在哪一类
    reasons = []
    if not intent_ok:
        reasons.append(f"意图判错({state.get('intent') or '空'}，应为{intent_expect})")
    if forbid_words:
        reasons.append(f"出现禁用表述({','.join(forbid_words)})")
    if tool_rate < 1.0:
        reasons.append(f"工具选错(缺{','.join(t for t in expect if t not in tools)})")
    if gb_rate < 1.0:
        reasons.append(f"取证维度不足(缺{','.join(g for g in gb_expect if g not in gb_used)})")
    if kw_rate < 0.5:
        reasons.append("判断错/结论未命中")
    if req_miss:
        reasons.append(f"结论落点不全(缺{','.join(req_miss)})")
    if forbid_hit:
        reasons.append(f"越界调用({','.join(forbid_hit)})")
    if not steps_ok:
        reasons.append(f"步数越界({steps}不在{lo}-{hi})")
    if blocked:
        reasons.append(f"未溯源数字({','.join(blocked)})")
    if fc_fail:
        reasons.append("工具调用格式错")
    if not deduped_ok:
        reasons.append("未命中幂等（应返回已存在工单）")

    return {
        "id": traj["id"],
        "type": traj["type"],
        "intent": state.get("intent", ""),
        "intent_ok": intent_ok,
        "tools": tools,
        "steps": steps,
        "tool_rate": round(tool_rate, 3),
        "gb_rate": round(gb_rate, 3),
        "kw_rate": round(kw_rate, 3),
        "kw_hit": kw_hit,
        "req_miss": req_miss,
        "steps_ok": steps_ok,
        "blocked": blocked,
        "fc_fail": fc_fail,
        "semantic_ok": semantic_ok,
        "reasons": reasons,
        "final_head": final[:160].replace("\n", " "),
    }


def main():
    only = None
    workers = 1
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = {x.strip().upper() for x in arg.split("=", 1)[1].split(",")}
        elif arg.startswith("--workers="):
            workers = max(1, int(arg.split("=", 1)[1]))

    targets = [t for t in TRAJECTORIES if not only or t["id"] in only]
    print(f"待评测 {len(targets)} 条，并发 {workers}\n")

    # 这一份 meta 是"这份报告是在什么条件下产生的"的唯一凭证。
    # 没有它，8 份历史报告之间无法比较：不知道换过模型、改过用例、还是改过 prompt。
    meta = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "commit": _git_commit(),
        "provider": PROVIDER,
        "model": MODEL,
        "base_url": BASE_URL,
        "cases_hash": _cases_hash(),
        "code_hash": _code_hash(),
        "cases_total": len(TRAJECTORIES),
        "evaluated": len(targets),
        "workers": workers,
        "use_checkpoint": workers == 1,
    }
    print(
        f"基线 commit={meta['commit'] or '(无)'}  model={MODEL}  "
        f"code_hash={meta['code_hash']}  cases_hash={meta['cases_hash']}\n"
    )

    # 建单类共享 mock QMS 的外部状态：W2 考的是"再建一次应返回已存在"，
    # 与 W1 并发会交叉，幂等结果不可信。强制串行，且排在并发批之后。
    serial = [t for t in targets if t["type"] == "建单"]
    parallel = [t for t in targets if t["type"] != "建单"]

    # 并发必须关掉 checkpointer：它是进程内单例、只有一条 psycopg 连接
    use_ckpt = (workers == 1)
    lock = threading.Lock()
    got = {}

    def one(traj):
        try:
            state = run(traj["input"], trace_run=False, use_checkpoint=use_ckpt)
        except Exception as exc:
            return traj, None, {
                "id": traj["id"], "type": traj["type"], "semantic_ok": False,
                "reasons": [f"运行异常:{type(exc).__name__}: {exc}"],
                "tools": [], "steps": 0, "tool_rate": 0, "gb_rate": 0,
                "kw_rate": 0, "steps_ok": False, "blocked": [], "fc_fail": True,
                "final_head": "",
            }
        return traj, state, judge(traj, state)

    def record(traj, state, r):
        with lock:
            got[traj["id"]] = r
            # 逐次追加落盘：report_eval.json 只保留最后一次，会覆盖历史，
            # 诊断"同一输入为什么这次对那次错"必须有每一次的结论正文。
            try:
                if state is None:
                    return
                rec = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "commit": meta["commit"],
                    "model": meta["model"],
                    "id": traj["id"],
                    "semantic_ok": r["semantic_ok"],
                    "kw_rate": r["kw_rate"],
                    "req_miss": r.get("req_miss", []),
                    "steps": r["steps"],
                    "tools": r["tools"],
                    "reasons": r["reasons"],
                    "final": state.get("final", "") or "",
                }
                with (ROOT / "eval" / "history.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass  # 落盘失败不能中断评测

    t0 = time.time()
    if parallel:
        print(f"[并发批] {len(parallel)} 条，workers={workers}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, t) for t in parallel]
            for f in as_completed(futs):
                record(*f.result())
    if serial:
        print(f"[串行批] {len(serial)} 条（建单类共享外部状态，必须串行）")
        for t in serial:
            record(*one(t))
    elapsed = time.time() - t0

    results = [got[t["id"]] for t in targets if t["id"] in got]

    for r in results:
        flag = "OK" if r["semantic_ok"] else "FAIL"
        print(f"[{r['id']}] {flag} 步数{r['steps']} 工具{len(r['tools'])}")
        if not r["semantic_ok"]:
            print(f"     原因: {'; '.join(r['reasons'])}")

    n = len(results)
    ok = sum(1 for r in results if r["semantic_ok"])
    fc = sum(1 for r in results if r["fc_fail"])
    blocked_n = sum(1 for r in results if r["blocked"])
    steps_bad = sum(1 for r in results if not r["steps_ok"])

    print("\n" + "=" * 60)
    print(f"语义正确率    {ok}/{n} = {ok / n:.1%}")
    print(f"工具命中率    {sum(r['tool_rate'] for r in results) / n:.3f}（平均）")
    print(f"维度覆盖率    {sum(r['gb_rate'] for r in results) / n:.3f}（平均）")
    print(f"终局命中率    {sum(r['kw_rate'] for r in results) / n:.3f}（平均）")
    print(f"步数越界      {steps_bad}/{n}")
    intent_bad = sum(1 for r in results if r.get("intent_ok") is False)
    print(f"意图判错      {intent_bad}/{n}")
    print(f"fc 失败       {fc}/{n}")
    print(f"未溯源数字    {blocked_n}/{n}")
    print(f"总耗时        {elapsed:.0f} s（平均 {elapsed / n:.1f} s/条）")

    # 报告从"一个裸数组"变成"带身份的结构体"：meta 说清这份报告在什么条件下产生，
    # summary 放聚合数，results 保留逐条明细。旧报告仍是裸数组，不要倒退着改它们。
    commit = meta["commit"] or "nogit"
    stamp = time.strftime("%Y%m%d_%H%M")
    summary = {
        "n": n,
        "semantic_ok": ok,
        "semantic_rate": round(ok / n, 4) if n else 0.0,
        "tool_rate": round(sum(r["tool_rate"] for r in results) / n, 4) if n else 0.0,
        "gb_rate": round(sum(r["gb_rate"] for r in results) / n, 4) if n else 0.0,
        "kw_rate": round(sum(r["kw_rate"] for r in results) / n, 4) if n else 0.0,
        "steps_bad": steps_bad,
        "intent_bad": intent_bad,
        "fc_fail": fc,
        "blocked_n": blocked_n,
        "elapsed_s": round(elapsed, 1),
    }
    report = {"meta": meta, "summary": summary, "results": results}
    text = json.dumps(report, ensure_ascii=False, indent=1)

    # 带 commit 的归档件：这一份只对应这一个代码版本，以后能和别的报告比
    out_full = ROOT / "eval" / f"report_{stamp}_{commit}.json"
    out_full.write_text(text, encoding="utf-8")
    # 同名副本保持老引用可用（README 与几篇 docs 都指向 report_eval.json）
    (ROOT / "eval" / "report_eval.json").write_text(text, encoding="utf-8")

    print(f"\n明细已写入 {out_full.name}（report_eval.json 同步为最新副本）")
    print("这一份要登记进 eval/报告状态.md，否则下次仍判断不了它能不能和别的报告比")


if __name__ == "__main__":
    main()
