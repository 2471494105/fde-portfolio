"""判定器严格度验证。

证明 final_required 真的能抓住"结论落点不全"的情况，
不联网、不调模型，直接构造 state 调 judge()。

背景：原判定器用 kw_rate >= 0.5 当门槛，H4 的 final_keywords 是
["1号工位", "光源"]，只要命中一个（0.5）就能判 OK。
实测出现过"提了1号工位、但没落到光源"却仍判 OK 的情况。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.run_eval import judge  # noqa: E402
from eval.trajectories import TRAJECTORIES  # noqa: E402

H4 = next(t for t in TRAJECTORIES if t["id"] == "H4")


def make_state(final, traj):
    gb = traj.get("expect_group_by", [])
    return {
        "tool_names": list(traj["expect_tools"]),
        "final": final,
        "steps": 8,
        "blocked": [],
        "evidence": [
            {"tool": "query_defect_log", "args": {"device_id": "CX-101", "group_by": g}, "result": {}}
            for g in gb
        ],
        "intent": traj.get("expect_intent") or "diagnose",
    }


CASES = [
    ("只提位置、漏掉光源（真实失败形态）",
     "问题出在1号工位，近7天整体误判率 23.89%，建议现场复核。"),
    ("位置 + 原因都有（正确结论）",
     "根因：1号工位光源超额定寿命，累计 8620h / 额定 8000h。"),
    ("位置 + 原因都有，但带空格（模型常见写法）",
     "**根因方向：1 号工位光源衰减（超额定寿命）+ 亮度两次下调**"),
    ("只提光源、漏掉位置（另一种落点不全）",
     "光源累计点亮 8620h，已超额定寿命 620h。"),
    ("位置和原因都缺（空泛结论）",
     "整体误判率为 23.89%，建议排查。"),
]

print("=" * 60)
print("一、H4（已设 final_required = ['1号工位', '光源']）")
print("=" * 60)
rows = []
for name, final in CASES:
    r = judge(H4, make_state(final, H4))
    rows.append((name, r))
    print(f"  {name}")
    print(f"    semantic_ok = {r['semantic_ok']} | kw_rate = {r['kw_rate']} | req_miss = {r['req_miss']}")
    if r["reasons"]:
        print(f"    reasons = {r['reasons']}")

print()
print("=" * 60)
print("二、对照组：未设 final_required 的老用例，行为必须不变")
print("=" * 60)
H5 = next(t for t in TRAJECTORIES if t["id"] == "H5")
ok_final = "标定漂移超阈值：11-08 起按天看出现陡变"
r5 = judge(H5, make_state(ok_final, H5))
print(f"  H5（正确结论）  semantic_ok = {r5['semantic_ok']} | kw_rate = {r5['kw_rate']} | req_miss = {r5['req_miss']}")
# H5 的 final_keywords 是 ['标定','漂移']，只命中一个时旧逻辑仍应判 OK（不受影响）
half_final = "标定记录都正常，怀疑是阈值问题"
r5b = judge(H5, make_state(half_final, H5))
print(f"  H5（只命中'标定'）semantic_ok = {r5b['semantic_ok']} | kw_rate = {r5b['kw_rate']} | req_miss = {r5b['req_miss']}")
print("  ↑ 此项应仍为 True：说明 final_required 只对显式声明它的用例生效")

print()
print("=" * 60)
print("三、断言")
print("=" * 60)
assert rows[0][1]["semantic_ok"] is False, "漏掉光源必须判失败"
assert rows[0][1]["req_miss"] == ["光源"], "缺失项应精确指向光源"
assert rows[1][1]["semantic_ok"] is True, "落点完整的结论必须判通过"
assert rows[2][1]["semantic_ok"] is True, "『1 号工位』带空格必须能命中，不能被空格打败"
assert rows[3][1]["semantic_ok"] is False, "漏掉工位必须判失败"
assert rows[4][1]["semantic_ok"] is False, "空泛结论必须判失败"
assert r5["semantic_ok"] is True, "老用例不受影响"
assert r5b["semantic_ok"] is True, "老用例仍按 0.5 阈值判，不能被连带收紧"
print("全部断言通过")
