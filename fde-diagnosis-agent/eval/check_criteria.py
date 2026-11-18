"""判据回归：10 条已知答案的真实样本，本地秒级复算。

为什么单独有这个：动 nodes.py 的判据代码只有两条验证路——
跑单测（只覆盖当场想到的形状）、重跑全量（3000 秒）。
中间这层是空的。太贵所以不会每次都跑，所以判据退化了下一次才发现。

这份夹具存的是 32704d4 那轮的真实 final + evidence，期望值人工核对过原文。
它抓的不是"判据能判对"，是"判据没退回去"。

用法：
    python eval/check_criteria.py            # 通过/失败 + 退出码
    python eval/check_criteria.py -v         # 附带每条的实际计数

约定：动 app/graph/nodes.py 里判据相关代码（_value_match / _match_item /
_verify_claims / _get_path）之前先跑这个。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.graph.nodes import _new_stats, _verify_claims  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "criteria_cases.json"


def rebuild_ev(strings):
    """把 history.jsonl 里 "[工具名{指纹}] {json}" 的字符串还原成 evidence 结构。

    verify 侧要的字段是 result（原始返回文本）、tool（身份）、sig（参数指纹）。
    生产代码里 result 带着 "[工具名] " 前缀（_TOOL_TAG 会剥掉），这里按同格式补上，
    否则走的是另一条解析路径，验的就不是生产行为。
    """
    ev = []
    for s in strings:
        i = s.find("}] ")
        if i < 0:
            continue
        head, body = s[1:i], s[i + 3:]
        tool = head.split("{")[0]
        # head 截到 "}" 之前，本身已不含花括号。再 -1 会吃掉指纹最后一个字符：
        # group_by=station 变成 group_by=statio，与真实指纹永远匹配不上，
        # 指纹收窄整层静默失效，全靠"值比较 + 逐数组试"兜着，验的就不是生产行为。
        ev.append({"tool": tool, "sig": head[len(tool) + 1:],
                   "result": f"[{tool}] {body}", "turn": 1})
    return ev


def as_doc(final):
    if isinstance(final, dict):
        return final
    t = str(final or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        d = json.loads(t)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def check(case):
    doc = as_doc(case["final"])
    stats = _new_stats()
    blocked, misquoted = [], []
    _verify_claims(doc, rebuild_ev(case["evidence"]), stats, blocked, misquoted)

    exp = case["expect"]
    fails = []
    for k in ("multi_ok", "multi_bad", "path_bad", "ref_ambiguous"):
        if k in exp and stats[k] != exp[k]:
            fails.append(f"{k}={stats[k]}，期望 {exp[k]}")
    if "misquoted_count" in exp and len(misquoted) != exp["misquoted_count"]:
        fails.append(f"misquoted 共 {len(misquoted)} 条，期望 {exp['misquoted_count']} 条")
    for frag in exp.get("misquoted_has") or []:
        if not any(frag in m for m in misquoted):
            fails.append(f"报错里没有「{frag}」，实际：{misquoted[:1]}")
    return fails, stats, misquoted


def main(argv):
    verbose = "-v" in argv
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    bad = 0
    for c in cases:
        fails, stats, misquoted = check(c)
        if fails:
            bad += 1
            print(f"FAIL  {c['id']}  {c['covers']}")
            for f in fails:
                print(f"        - {f}")
            if verbose:
                print(f"        实际 counts: multi={stats['multi_ok']}/{stats['multi_bad']} "
                      f"path_ok={stats['path_ok']} path_bad={stats['path_bad']} "
                      f"amb={stats['ref_ambiguous']} misq={len(misquoted)}")
        else:
            print(f"PASS  {c['id']}  {c['covers']}")
            if verbose:
                print(f"        multi={stats['multi_ok']}/{stats['multi_bad']} "
                      f"path_ok={stats['path_ok']} path_bad={stats['path_bad']} "
                      f"amb={stats['ref_ambiguous']} misq={len(misquoted)}")
        if verbose:
            for m in misquoted:
                print(f"        ! {m[:90]}")
    print()
    print(f"{len(cases) - bad}/{len(cases)} 通过")
    if bad:
        print("\n判据退化了。先看上面哪条 FAIL，再去动 nodes.py。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
