"""参考解：一份"已知能通过全部判据"的快照，用来给判据本身做回归。

一手依据
--------
Anthropic《Demystifying evals for AI agents》Step 2 原话：

    For each task, it's useful to create a **reference solution**: a known
    working output that passes all graders. This proves that the task is
    solvable and verifies graders are correctly configured.

为什么这套系统特别需要它
------------------------
判据和被测对象已经出过四次同源（见 Obsidian《判据与被测对象同源》）。
其中一种形态是"判据把好答案判红"。同一篇文档给的例子是 CORE-Bench：
一个严格的字符串比较把 "96.12" 判成错（它期望 "96.124991…"），
修掉这类 rigid grading 之后，Claude Opus 4.5 的成绩从 42% 跳到 95%。

真正难的地方不是"判据会写错"，而是**判据写错时报告看起来完全正常**：
你没有办法从一张 FAIL 的表上区分"模型不行"和"判据太严"。
参考解就是那个对照组——它是已知的好答案，
判据把它判红，就说明问题在判据，与被测模型无关。

存什么
------
判据 judge() 依赖的 state 白名单 + 完整用例定义。不存 messages：
体积大，且判据只读 final / evidence 这些已经落定的字段。

用例定义一起存，因为判据是按 traj 判的。用例改了，旧参考解就不再代表
"这道题的好答案"，verify 会把它标 stale 而不是算作判据回归。

用法
----
    python eval/reference.py --verify     # 用当前判据重判全部参考解

由 run_eval 在每次评测收尾时自动落盘通过用例，所以平时不用手动 save。
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "eval" / "reference"

# judge() 读到的 state 字段，逐条对齐 run_eval.judge 里的 state.get(...)。
# 少一个就会让参考解在 verify 时"缺字段走默认值"，凭空通过或凭空失败——
# 那等于给判据回归本身埋一个同源缺陷。
STATE_KEYS = (
    "tool_names", "final", "final_text", "steps",
    "blocked", "misquoted", "cite_stats", "stop_reason", "check_retry",
    "unknown", "evidence",
    "intent", "intent_conf", "route_by", "model_intent",
)


def snapshot(state) -> dict:
    """从完整 state 里摘出判据需要的那部分。"""
    return {k: state.get(k) for k in STATE_KEYS}


def fingerprint(traj) -> str:
    """单个用例定义的指纹：用例一改，旧参考解就不能再冒充"这道题的好答案"。"""
    import hashlib
    keys = ("id", "input", "turns", "expect_tools", "forbid_tools",
            "expect_group_by", "final_keywords", "expect_ask",
            "expect_intent", "expect_deduped", "steps_range")
    payload = json.dumps({k: traj.get(k) for k in keys},
                         ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def save_reference(traj, state, meta, ref_dir=REF_DIR) -> str:
    """把一条通过的用例存成参考解。失败不抛——评测收尾不该因为落盘挂掉。"""
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
        path = ref_dir / f"{traj['id']}.json"
        path.write_text(json.dumps({
            "id": traj["id"],
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "traj_fp": fingerprint(traj),
            "traj": {k: v for k, v in traj.items()},
            "state": snapshot(state),
            "meta": {
                "commit": (meta or {}).get("commit"),
                "code_hash": (meta or {}).get("code_hash"),
                "cases_hash": (meta or {}).get("cases_hash"),
            },
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return str(path)
    except Exception:
        return ""


def load_references(ref_dir=REF_DIR) -> list:
    if not ref_dir.exists():
        return []
    out = []
    for p in sorted(ref_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def verify(trajs, ref_dir=REF_DIR) -> dict:
    """用当前判据重判全部参考解。

    返回 {"ok": [...], "stale": [...], "failed": [...], "missing": [...]}：

    ok      当前判据仍然判它通过——判据没被改坏
    stale   用例定义变了，旧参考解不再代表这道题的好答案，需要重新跑一次生成
    failed  用例没变，但当前判据把它判红了。**两种可能，必须人来分辨**：
            判据改严了（有意的，那就重新生成参考解），或判据改错了（这轮要修）
    missing 某个当前用例还没有参考解
    """
    from eval.run_eval import judge  # 延迟导入，避免循环

    by_id = {r["id"]: r for r in load_references(ref_dir)}
    current = {t["id"]: t for t in trajs}
    result = {"ok": [], "stale": [], "failed": [], "missing": []}

    for cid, traj in current.items():
        ref = by_id.get(cid)
        if not ref:
            result["missing"].append(cid)
            continue
        if ref.get("traj_fp") != fingerprint(traj):
            result["stale"].append(cid)
            continue
        try:
            r = judge(traj, ref.get("state") or {})
            ok = bool(r.get("semantic_ok"))
            reasons = r.get("reasons") or []
        except Exception as exc:
            ok, reasons = False, [f"重判异常:{type(exc).__name__}: {exc}"]
        if ok:
            result["ok"].append(cid)
        else:
            result["failed"].append({"id": cid, "reasons": reasons})

    for cid in by_id:
        if cid not in current:
            result["stale"].append(cid)  # 用例被删了
    return result


def main(argv) -> int:
    if "--verify" not in argv:
        print(__doc__)
        return 0
    from eval.trajectories import TRAJECTORIES
    res = verify(TRAJECTORIES)

    print(f"参考解校验：通过 {len(res['ok'])} / "
          f"失效 {len(res['stale'])} / 判红 {len(res['failed'])} / "
          f"缺 {len(res['missing'])}（共 {len(TRAJECTORIES)} 条用例）")
    if res["failed"]:
        print("\n判据把这些已知的好答案判红了——先怀疑判据，不要先怀疑模型：")
        for f in res["failed"]:
            print(f"  [{f['id']}] {'; '.join(f['reasons'])}")
    if res["stale"]:
        print(f"\n以下用例改过或已删，旧参考解不再代表本题，需重跑生成："
              f"\n  {', '.join(res['stale'])}")
    if res["missing"]:
        print(f"\n以下用例还没有参考解（跑一次全量会自动落盘）："
              f"\n  {', '.join(res['missing'])}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
