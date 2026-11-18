"""拿 verify / check 写出来的真实字段值，补进 state 样本。

为什么单独一个脚本：
  `dump_state_samples.py` 跑真实会话，但模型 403 时全部降级到 lookup，
  诊断路径（plan→act→check→verify）压根不执行，于是 cite_stats / cite_fail /
  blocked / misquoted / check_* 这些字段永远取到空值。空值没有说明力。

  verify 和 check 是**纯程序节点**，不调模型。所以可以直接构造 state 喂进去，
  dump 它们写出来的值。这不是"假装跑了一遍"：函数确实执行了，写出来的就是
  真实结构，和单测互证。

三个构造场景，各自打中不同的分支：
  mixed    一条值错、一条路径不存在、一条 ref=null、一条 ref 格式坏 —— 一次覆盖
           大部分 cite_fail code
  retry    check_retry=0 且有伪引用 → check_action="retry"
  nullswap check_retry 已达上限，且 unsourced 反而涨了 → 抓"改成 null 蒙过去"
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.nodes import check, verify  # noqa: E402

OUT = ROOT / "docs" / "state_samples_20261118.json"

# verify 读的是 evidence[i]["result"]，它必须是一段能 json.loads 的字符串
PROFILE = {
    "device_id": "CX-101", "state": "found", "line": "3号线",
    "station": "1-3号工位", "camera_model": "In-Sight 2801",
    "light_model": "CA-DC21E", "maintenance_cycle_days": 90, "log_count": 200,
}
DEFECT = {"device_id": "CX-101", "state": "found", "total": 113,
          "false_positive_count": 27, "false_positive_rate": 23.89}


def ev(tool, args, result, turn=1, no=1):
    return {"tool": tool, "args": args, "sig": "",
            "result": json.dumps(result, ensure_ascii=False),
            "truncated": False, "ev_no": no, "turn": turn}


BASE_EV = [
    ev("query_device_profile", {"device_id": "CX-101"}, PROFILE, no=1),
    ev("query_defect_log", {"device_id": "CX-101"}, DEFECT, no=2),
]

MIXED_FINAL = {
    "conclusion": "按设备档案与最近 7 天的误判统计列出如下，不做根因判断。",
    "claims": [
        {"text": "保养周期 90 天", "value": 90, "unit": "天",
         "ref": "query_device_profile#maintenance_cycle_days"},
        {"text": "保养周期 91 天", "value": 91, "unit": "天",
         "ref": "query_device_profile#maintenance_cycle_days"},
        {"text": "未知字段 5", "value": 5, "unit": "条",
         "ref": "query_device_profile#no_such_field"},
        {"text": "推定 3 台", "value": 3, "unit": "台", "ref": None},
        {"text": "编号 7", "value": 7, "unit": "", "ref": "7#"},
    ],
    "advice": ["核对设备编号后重查"],
}


def run_case(name, state):
    v = verify(state)
    c = check(dict(state, **{"final": v.get("final", state.get("final"))}))
    rec = {
        "verify": {k: v.get(k) for k in
                   ("blocked", "misquoted", "cite_stats", "cite_fail",
                    "unknown", "conflicts", "final_text")},
        "check": {k: c.get(k) for k in
                  ("check_action", "check_retry", "check_notes",
                   "check_unsourced0", "check_ev")},
    }
    print(f"\n=== {name}")
    print(f"  verify blocked={len(rec['verify']['blocked'] or [])} "
          f"misquoted={len(rec['verify']['misquoted'] or [])} "
          f"cite_fail={len(rec['verify']['cite_fail'] or [])}")
    print(f"  check action={rec['check']['check_action']} "
          f"retry={rec['check']['check_retry']} ev={rec['check']['check_ev']}")
    return rec


def main():
    base = {"turn": 1, "evidence": BASE_EV,
            "tool_names": ["query_device_profile", "query_defect_log"],
            "unknown": [], "conflicts": [], "final": MIXED_FINAL}

    cases = {
        "verify_mixed": base,
        "check_retry": dict(base),
        "check_nullswap": dict(base),
    }
    # nullswap 要模拟"回环之后没有减少未溯源，反而多了"
    cases["check_nullswap"]["check_retry"] = 1
    cases["check_nullswap"]["check_unsourced0"] = 0

    out = {}
    for name, st in cases.items():
        out[name] = run_case(name, st)

    data = json.loads(OUT.read_text(encoding="utf-8"))
    data["programmatic"] = out
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已并入 {OUT}")

    st = out["verify_mixed"]["verify"]["cite_stats"]
    print(f"\ncite_stats 共 {len(st)} 个 key")
    print(json.dumps(out["verify_mixed"]["verify"]["cite_fail"],
                     ensure_ascii=False, indent=1))
    print(json.dumps(out["verify_mixed"]["verify"]["blocked"],
                     ensure_ascii=False, indent=1))
    print(json.dumps(out["verify_mixed"]["verify"]["misquoted"][:2],
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
