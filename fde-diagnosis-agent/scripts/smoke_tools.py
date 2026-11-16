"""7 个工具的冒烟测试：逐个真实调库，检查返回结构和关键数字。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.tools import TOOL_IMPLS  # noqa: E402


def run(name, **kwargs):
    print(f"\n--- {name} {kwargs} ---")
    try:
        res = TOOL_IMPLS[name](**kwargs)
    except Exception as exc:
        print(f"!! 异常: {type(exc).__name__}: {exc}")
        return None
    text = json.dumps(res, ensure_ascii=False, default=str)
    print(text[:600] + ("..." if len(text) > 600 else ""))
    return res


def main():
    run("query_device_profile", device_id="CX-101")

    r = run("query_defect_log", device_id="CX-101", group_by="station")
    if r and "groups" in r:
        top = r["groups"][0]
        print(f"  [检查] 1号工位占比最高: {top['key']} = {top['rate']:.1%}")

    r = run("query_defect_log", device_id="CX-102", group_by="batch")
    if r and "groups" in r:
        print(f"  [检查] 批次集中: {r['groups'][0]['key']} = {r['groups'][0]['rate']:.1%}")

    r = run("query_calibration", device_id="CX-104")
    if r and r.get("records"):
        print(f"  [检查] 最近标定漂移: {r['records'][0]['drift_mm']} mm, 趋势 {r['drift_trend']}")

    r = run("query_light_usage", device_id="CX-101")
    if r and "over_life_hours" in r:
        print(f"  [检查] 超出额定 {r['over_life_hours']} 小时")

    r = run("search_manual", query="光源亮度设定标准值", top_k=2)
    if r and r.get("chunks"):
        print(f"  [检查] 命中 {len(r['chunks'])} 块, 首条来源: {r['chunks'][0]['source_doc']}")

    r = run("check_doc_conflict", param_name="光源亮度设定")
    if r:
        print(f"  [检查] has_conflict={r.get('has_conflict')}, 取值数={r.get('value_count')}")

    # 幂等验证：连续建两次单
    payload = dict(
        device_id="CX-101",
        root_cause="光源超额定寿命导致亮度衰减",
        root_cause_code="light_degradation",
        severity="high",
        actions=["更换光源", "复标定并验证 50 件标准样件"],
        evidence_refs=["1号工位误判率57.6%", "光源累计8620h超额定8000h", "亮度两次下调85→80→65"],
    )
    first = run("create_work_order", **payload)
    second = run("create_work_order", **payload)
    if first and second:
        print(f"  [检查] 幂等: 第一次 deduped={first['deduped']}, 第二次 deduped={second['deduped']}, "
              f"单号一致={first['order_id'] == second['order_id']}")


if __name__ == "__main__":
    main()
