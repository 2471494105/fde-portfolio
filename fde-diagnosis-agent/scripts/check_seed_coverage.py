"""判据断言的标识，必须能在种子文件里搜到。

为什么需要这道 0.2 秒的闸（2026-11-20 G3）：
    I9 期望"答出一张工单"，判据要求结论里出现 `WO-`。而 `data/seed/002_data.sql`
    里从来没有 work_orders 的 INSERT——那个 `WO-` 的存在依赖别的用例建单。
    清库后跑全量，六轮每一轮都红，而每一轮看起来都像"模型不会查工单"。

    判据是对的，被测前提不在。这类缺口不会抛异常、不打日志、没有一条测试会红，
    只能靠"把判据要的标识拿去种子里搜一遍"来发现。

判什么：
    扫 eval/trajectories.py 里 final_keywords / final_required 的值，
    抽出标识形态，逐个在 data/seed/*.sql 里搜。
    搜不到 → 这条用例在断言一个种子没有的环境事实，报红并指出依赖它的用例。

标识形态（两种）：
    WO-                      前缀式（只给家族前缀，不给具体号）
    CX-101 / E-202 / B20261105 / WO-20261105-001 / WI-QC-015   具体号

次级来源：
    data/manual_seed.py 里的手册片段（WI-QC-015、PM-2026 在这份文件里，
    不在 002_data.sql）。它算"会进库"但不进 seed_hash，所以只报警告、不报红：
    真要断言这些标识，先把手册入库这一步做成确定的。

用法：
    .\\venv\\Scripts\\python.exe -X utf8 scripts\\check_seed_coverage.py
    .\\venv\\Scripts\\python.exe -X utf8 scripts\\check_seed_coverage.py -v

退出码：0 = 全部搜到（或只有次级来源的警告）；1 = 有标识在种子 SQL 里根本不存在。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.trajectories import TRAJECTORIES  # noqa: E402

SEED_DIR = ROOT / "data" / "seed"
DOC_SEED = ROOT / "data" / "manual_seed.py"

# 具体号：字母前缀 + 数字段，允许再跟数字段（WO-20261105-001）。
ID_RE = re.compile(r"[A-Za-z]{1,8}-\d{2,8}(?:-\d{2,8})*|[A-Za-z]\d{6,8}")
# 前缀式：整段就是一个"字母-"，用来表达"任何一张这个家族的号都行"（WO-）。
PREFIX_RE = re.compile(r"^[A-Za-z]{1,8}-$")

# 断言字段：判据真的会拿它判红的地方。input / note 里出现的号不算——
# 那些只是用户怎么说，不是判据要求结论必须出现什么。
ASSERT_FIELDS = ("final_keywords", "final_required")


def _load_sources(paths):
    """[(名字, 文本)]。读不到的文件跳过并留一行提示，不静默。"""
    out = []
    for p in paths:
        try:
            out.append((p.name, p.read_text(encoding="utf-8", errors="replace")))
        except OSError as e:
            print(f"!! 读不到 {p}：{e}")
    return out


def seed_files(only=None):
    """种子 SQL 主文件。刻意排除 .bak_*：备份不是初态，算进来会掩盖"主文件里没有"。

    only 只给自测用：拿 .bak_before_wo_seed 这种"改之前"的快照当语料，
    验这道闸在真缺东西时会不会响。正常跑不传它。
    """
    if only:
        p = SEED_DIR / only
        return [p] if p.exists() else []
    return sorted(p for p in SEED_DIR.glob("*.sql") if ".bak_" not in p.name)


def asserted_ids(traj):
    """这条用例断言了哪些标识。

    只取断言字段；返回 [(标识, 字段)]，identifier 去重但保留字段，
    因为报红时要能说清"是 final_required 要求的，还是 final_keywords 里同义任选之一"。
    """
    found = []
    for field in ASSERT_FIELDS:
        for val in traj.get(field) or []:
            s = str(val)
            if ID_RE.fullmatch(s) or PREFIX_RE.match(s):
                found.append((s, field))
    return found


def check(only=None):
    """返回 (missing, doc_only, checked_n, deps)。deps 是 {标识: [(用例, 字段)]}。"""
    sql_srcs = _load_sources(seed_files(only))
    doc_srcs = _load_sources([DOC_SEED]) if DOC_SEED.exists() else []
    sql_all = "\n".join(t for _n, t in sql_srcs)
    doc_all = "\n".join(t for _n, t in doc_srcs)

    deps = {}
    for traj in TRAJECTORIES:
        for ident, field in asserted_ids(traj):
            deps.setdefault(ident, []).append((traj["id"], field))

    missing, doc_only = [], []
    for ident in sorted(deps):
        if ident in sql_all:
            continue
        if ident in doc_all:
            doc_only.append(ident)
        else:
            missing.append(ident)
    return missing, doc_only, len(deps), deps


def main(argv):
    verbose = "-v" in argv
    only = next((a.split("=", 1)[1] for a in argv if a.startswith("--seed-file=")), None)
    missing, doc_only, n_ids, deps = check(only)
    files = ", ".join(p.name for p in seed_files(only)) or "(无)"

    print(f"种子覆盖自检：{n_ids} 个标识，来源 {files}")

    for ident in doc_only:
        where = "; ".join(f"{cid}.{f}" for cid, f in deps[ident])
        print(f"?  {ident} 只在 data/manual_seed.py 里（不在种子 SQL）——依赖它：{where}")
        print("   手册入库若不确定，这条断言的绿是环境给的，不是模型做对的")

    if missing:
        for ident in missing:
            where = "; ".join(f"{cid}.{f}" for cid, f in deps[ident])
            print(f"!! {ident} 在种子 SQL 里搜不到——依赖它：{where}")
        print(f"\n{len(missing)} 个标识不存在于种子。只有两种解释：")
        print("  ① 种子缺了 → 补 data/seed_generate.py（别手改 002_data.sql）")
        print("  ② 这条用例不该断言它 → 改用例，别放宽判据")
        print("  先分清是哪种再动手：判据可能是对的。")
        if verbose:
            print("\n搜过的文件：")
            for p in seed_files():
                print(f"  {p}")
        return 1

    if verbose:
        for ident in sorted(deps):
            print(f"OK {ident:<16} {'; '.join(f'{c}.{f}' for c, f in deps[ident])}")
    print(f"通过（{n_ids - len(doc_only)} 个标识在种子 SQL 里，{len(doc_only)} 个只在手册源）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
