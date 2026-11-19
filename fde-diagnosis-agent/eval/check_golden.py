r"""golden 自检：证明「这三条用例可解」且「判据刻度是对的」。

替代原来 eval/reference/ 的做法。原来那套：只在模型通过时写、每轮覆盖、
且 run_eval.py 全程没调用过 load_references —— 结果是 26 个文件混着 5 个 code_hash，
失败的那几条永远停在旧版本，它既不能证明可解，也不能验证判据。

这里的 golden 是**人工写的标准答案**：claims 逐条带 ref，直接喂给 _verify_claims。
判据若把这些 claim 判错，说明是判据坏了，不是模型不行。
每次改判据都要重跑本脚本；它变红时第一反应必须是查判据。

跑法：.\venv\Scripts\python.exe -X utf8 eval\check_golden.py
"""
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.graph.nodes import _verify_claims          # noqa: E402
from app.tools import diagnosis, manual              # noqa: E402

GOLDEN_DIR = os.path.join(ROOT, "eval", "golden")

# 每条用例需要哪些证据。扩展新用例时在这里补一行即可。
# 参数必须具名传：query_defect_log 的第三位是 time_to，不是 group_by。
EVIDENCE = {
    "H1": [
        ("query_device_profile", lambda: diagnosis.query_device_profile("CX-101")),
        ("query_defect_log", lambda: diagnosis.query_defect_log(device_id="CX-101", group_by="station")),
        ("query_light_usage", lambda: diagnosis.query_light_usage("CX-101")),
    ],
    "T1": [
        ("check_doc_conflict", lambda: manual.check_doc_conflict("光源亮度设定")),
    ],
    "T3": [],
}


def check_one(g):
    cid = g["id"]
    ev = []
    for tool, fn in EVIDENCE.get(cid, []):
        try:
            ev.append({"tool": tool, "result": fn()})
        except Exception as e:                      # 证据本身取不到就是 hard fail
            return cid, [f"证据 {tool} 调用失败：{type(e).__name__}: {e}"], {}

    # 只调了 golden 声明的工具，且都真的调到了
    got_tools = [e["tool"] for e in ev]
    missing = [t for t in g.get("tools_called", []) if t not in got_tools]
    if missing:
        return cid, [f"声明的工具未准备到证据：{missing}"], {}

    stats = defaultdict(int)
    blocked, misquoted, fails = [], [], []
    doc = {"conclusion": g.get("final_text", ""), "claims": g.get("claims") or []}
    _verify_claims(doc, ev, stats, blocked, misquoted, fails)

    problems = []
    for f in fails:
        problems.append(f"[{f['code']}] {f['note']}")
    if stats["unsourced"]:
        problems.append(f"unsourced={stats['unsourced']}（有断言没出处）")
    if stats["ref_bad"]:
        problems.append(f"ref_bad={stats['ref_bad']}（ref 指不到字段）")
    if stats["derived_bad"]:
        problems.append(f"derived_bad={stats['derived_bad']}（自带算式核验不通过）")

    info = {
        "qty": stats["qty"],
        "cited": stats["cited_ok"] if "cited_ok" in stats else None,
        "unsourced": stats["unsourced"],
        "derived_ok": stats["derived_ok"],
        "blocked": len(blocked),
    }
    return cid, problems, info


def main():
    files = sorted(f for f in os.listdir(GOLDEN_DIR) if f.endswith(".json"))
    if not files:
        print("eval/golden 下没有 golden 文件")
        return 1

    bad = 0
    for fn in files:
        with open(os.path.join(GOLDEN_DIR, fn), encoding="utf-8") as fh:
            g = json.load(fh)
        cid, problems, info = check_one(g)
        if problems:
            bad += 1
            print(f"FAIL  {cid}  qty={info.get('qty')}")
            for p in problems:
                print(f"        {p}")
        else:
            print(f"PASS  {cid}  qty={info.get('qty')} unsourced=0 blocked={info.get('blocked')}")

    print(f"\n{len(files) - bad}/{len(files)} 通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
