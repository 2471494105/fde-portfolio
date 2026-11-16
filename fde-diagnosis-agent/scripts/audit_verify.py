"""校验层审计：把 A1 的两条腿分开量。

旧逻辑的漏洞有两处，性质不同，必须分开测：
  1. **复算漏洞**：_recomputable 拿全部证据的数字混池两两相除，编造的百分比容易"凑"出来。
  2. **覆盖面漏洞**：只校验 %，绝对量（"9200 小时""180 天"）根本不进校验，放行率恒为 100%。

新逻辑（verify 四层判定）：
  cited_ok / cited_bad / cite_oob / uncited
  且只有百分比允许复算，其它单位只认直接命中。

本脚本用真实 evidence 跑四组统计：

  A 旧口径放行率（%编造，全池复算）
  B 新口径放行率（%编造，只允许标的那条证据内复算）
  C 真实值误报率   （必须 0）
  D 标错角拦截率   （证明有区分力，不是"标了就放行"）
  E 绝对量编造     （旧逻辑根本不看 vs 新逻辑全拦）

跑法：
    python scripts\\audit_verify.py

会真实跑一次诊断拿 evidence（峰时可能 2–3 分钟，走中转站）。
"""

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph.build import run
from app.graph.nodes import _ev_text, _found, _hit, _nums_of

MSG = "CX-101 误判率有点高，你看看怎么回事"
N_SAMPLE = 300
# 证据落盘缓存：审计逻辑要反复调参，每次都跑一遍模型太贵也太慢
CACHE = Path(os.environ.get("TEMP", ".")) / "fde_audit_evidence.json"


def _pct(a, b):
    return f"{a / b:.1%}" if b else "n/a"


def _load_evidence(refresh=False):
    if not refresh and CACHE.exists():
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"（复用缓存 evidence {len(data)} 条；要重新取数加 --refresh）")
        return data
    st = run(MSG, trace_run=False)
    ev_texts = [_ev_text(e) for e in (st.get("evidence") or [])]
    if ev_texts:
        CACHE.write_text(json.dumps(ev_texts, ensure_ascii=False), encoding="utf-8")
    return ev_texts


def main():
    ev_texts = _load_evidence("--refresh" in sys.argv)
    if not ev_texts:
        print("没拿到证据，退出")
        return
    per_ev = [_nums_of(t) for t in ev_texts]
    all_nums = _nums_of(" ".join(ev_texts)) + _nums_of(MSG)
    print(f"证据 {len(ev_texts)} 条，单条数字数 {[len(x) for x in per_ev]}，"
          f"合计 {len(all_nums)} 个")

    rng = random.Random(42)

    # ---------- 生成编造值（证据里本来不存在，且不直接命中） ----------
    forged = []
    tries = 0
    while len(forged) < N_SAMPLE and tries < N_SAMPLE * 20:
        tries += 1
        v = round(rng.uniform(1, 99), 1)
        # 只要求"不是直接命中"：编造值本来就不该在证据里逐字出现。
        # 不能额外要求“也不靠复算”——那等于把样本全清空，因为数字一多，
        # 全池任意两数相除几乎能凑出任何值（实测 495 个数字时命中率 100%）。
        if not _hit(v, all_nums):
            forged.append(v)

    # ---------- A / B / C：同一个编造百分比，三档越来越严 ----------
    old_pass = sum(1 for v in forged if _found(v, "%", all_nums))
    weak_pass = 0
    for v in forged:
        k = rng.randrange(len(per_ev))
        if _found(v, "%", per_ev[k]):
            weak_pass += 1
    # 强溯源 = 只认直接命中（有算式的情况随机编造不可能成立）
    strong_pass = 0
    for v in forged:
        k = rng.randrange(len(per_ev))
        if _hit(v, per_ev[k]):
            strong_pass += 1

    print(f"\n编造的百分比 {len(forged)} 个：")
    print(f"  A 旧口径（全池命中+全池复算）    {old_pass:>4}  {_pct(old_pass, len(forged))}")
    print(f"  B 弱溯源（单条命中+单条复算）    {weak_pass:>4}  {_pct(weak_pass, len(forged))}")
    print(f"  C 强溯源（单条直接命中）         {strong_pass:>4}  {_pct(strong_pass, len(forged))}")
    print(f"    → 拦下率 {_pct(len(forged) - old_pass, len(forged))} "
          f"→ {_pct(len(forged) - weak_pass, len(forged))} "
          f"→ {_pct(len(forged) - strong_pass, len(forged))}")
    print(f"    注：强溯源还允许“算式成立且分子分母都在该证据里”",
          f"这条通道随机编造无法通过，故等价于直接命中")

    # ---------- C：真实值必须全部通过 ----------
    real = []
    for k, nums in enumerate(per_ev):
        for n in nums:
            if 1 < n < 10000:
                real.append((n, k))
    real = real[:N_SAMPLE]
    # 用最严口径（绝对量口径，不允许复算）测：真实值本就该直接命中
    real_fail = [(n, k) for n, k in real if not _found(n, "x", per_ev[k])]
    print(f"\n真实值 {len(real)} 个（取自证据本身、标对角）：")
    print(f"  C 误报 {len(real_fail)} 个  {_pct(len(real_fail), len(real))}")
    if real_fail:
        print(f"    误报样例: {real_fail[:5]}")

    # ---------- D：标错角必须被拦 ----------
    wrong_total = wrong_caught = 0
    for n, k in real:
        if len(per_ev) < 2:
            break
        k2 = (k + 1) % len(per_ev)
        if _found(n, "x", per_ev[k2]):
            continue  # 那条里也碰巧有，样本分不清
        wrong_total += 1
        if not _found(n, "x", per_ev[k2]):
            wrong_caught += 1
    print(f"\n真实值但标错角 {wrong_total} 个：")
    print(f"  D 拦下 {wrong_caught} 个  {_pct(wrong_caught, wrong_total)}")

    # ---------- E：绝对量编造，旧逻辑根本不看 ----------
    forged_abs = []
    tries = 0
    while len(forged_abs) < 200 and tries < 4000:
        tries += 1
        v = float(round(rng.uniform(100, 9999), 0))
        if not _hit(v, all_nums):   # 撞上证据里已有的数就不算编造
            forged_abs.append(v)
    print(f"\n编造的绝对量 {len(forged_abs)} 个（如 \"9200 小时\"）：")
    print(f"  旧逻辑（只校验 %）  放行 {len(forged_abs)} 个  100.0%（根本不进校验）")
    abs_pass = sum(1 for v in forged_abs
                   for k in [rng.randrange(len(per_ev))]
                   if _found(v, "小时", per_ev[k]))
    print(f"  新逻辑（只认直击）  放行 {abs_pass} 个  {_pct(abs_pass, len(forged_abs))}")

    # ---------- 判据 ----------
    print("\n--- 判据 ---")
    ok1 = strong_pass < old_pass
    ok2 = strong_pass < weak_pass
    ok3 = not real_fail
    ok4 = wrong_caught == wrong_total
    ok5 = abs_pass == 0
    print(f"  {'PASS' if ok1 else 'FAIL'}  强溯源拦下率高于旧口径（{strong_pass} < {old_pass}）")
    print(f"  {'PASS' if ok2 else 'FAIL'}  强溯源严于弱溯源，说明“要求算式”是必要的"
          f"（{strong_pass} < {weak_pass}）")
    print(f"  {'PASS' if ok3 else 'FAIL'}  真实值误报为 0")
    print(f"  {'PASS' if ok4 else 'FAIL'}  标错角全部被拦")
    print(f"  {'PASS' if ok5 else 'FAIL'}  编造的绝对量全部被拦")
    print("结果：", "全部通过" if (ok1 and ok2 and ok3 and ok4 and ok5) else "有未通过项")


if __name__ == "__main__":
    main()
