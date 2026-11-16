"""7 个工具逐个计时，定位"为什么有的 0.1s 有的 100s"。

轨迹里 query_device_profile 稳定 0.1s，而 query_calibration 出现过 117.5s。
同一套 psycopg 连接方式、同一个库，不可能相差三个数量级——
要么是查询本身有问题（大表扫描/无索引），要么是连接建立有问题。
"""

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app.tools import TOOL_IMPLS

CASES = [
    ("query_device_profile", {"device_id": "CX-101"}),
    ("query_defect_log", {"device_id": "CX-101", "group_by": "station"}),
    ("query_defect_log", {"device_id": "CX-101", "group_by": "day"}),
    ("query_calibration", {"device_id": "CX-101", "limit": 6}),
    ("query_light_usage", {"device_id": "CX-101"}),
    ("search_manual", {"query": "光源亮度设定标准值", "top_k": 4}),
    ("check_doc_conflict", {"param_name": "光源亮度"}),
]


def main():
    print(f"{'工具':26s} {'第1次':>9s} {'第2次':>9s} {'已热':>9s}  说明")
    print("-" * 78)
    for name, args in CASES:
        impl = TOOL_IMPLS.get(name)
        if impl is None:
            print(f"{name:26s} 未注册")
            continue
        ts = []
        for _ in range(3):
            t0 = time.perf_counter()
            try:
                impl(**args)
            except Exception as e:
                ts.append(None)
                print(f"   {name} 失败: {type(e).__name__} {str(e)[:60]}")
                break
            ts.append(time.perf_counter() - t0)
        if len(ts) == 3 and all(t is not None for t in ts):
            note = "首次含连接建立" if ts[0] > ts[1] * 3 else ""
            print(f"{name:26s} {ts[0]:8.3f}s {ts[1]:8.3f}s {ts[2]:8.3f}s  {note}")


if __name__ == "__main__":
    main()
