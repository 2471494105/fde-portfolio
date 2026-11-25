"""两源取同一个对象，逐字段比。

这不是单元测试，是**交付前的验收动作**。客户说「你的系统能连我们的导出文件」，
口头承诺不算数，要有一份「同一台设备，两源逐字段一致」的证据。

字段分两类，处理方式不同：
- 两源都能声明（档案 8 列）：逐个比，不一致即失败；
- 只有一个源能声明（log_count / latest_log_at 来自 defect_logs 聚合）：
  显式跳过并打印原因。硬比会把「源没有这张表」记成「数据不一致」，
  那是假缺陷，比漏检更误导人。

退出码：0 全一致 / 1 有差异 / 2 源不可用。
"""

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import get_row_source, get_source  # noqa: E402
from app.tools.diagnosis import query_device_profile  # noqa: E402

TABLE = "devices"
KEY_COLUMN = "device_id"

SHARED_COLUMNS = [
    "device_id",
    "line",
    "station",
    "camera_model",
    "lens_model",
    "light_model",
    "install_date",
    "maintenance_cycle_days",
]

CURSOR_ONLY_COLUMNS = ["log_count", "latest_log_at"]

# 日期字段按「是不是同一天」比，不按字符串比。
# 实测差异：pg 侧 query_device_profile 的 _fmt 把 date 也补成
# "2025-03-12 00:00:00"（既有行为，改它要动 BEHAVIOR_FILES 内的 diagnosis.py），
# csv 侧给的是 "2025-03-12"。这不是数据不一致，是表示层差异。
# 让 CSV 去补 "00:00:00" 是错的——那等于让数据源迁就上层的显示格式；
# 数据源给原始值、上层负责格式化，方向不能反。
DATE_COLUMNS = {"install_date"}


def norm(value) -> str:
    """两侧比较前统一成字符串。db 给 int / csv 给 str，不该算差异。"""
    return "" if value is None else str(value).strip()


def norm_date(value) -> str:
    """把日期统一成 YYYY-MM-DD。

    解析不了就退回原字符串——宁可暴露差异，也不静默放过。
    """
    text = norm(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("T", " ")).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def main() -> int:
    cursor_src = get_source()
    row_src = get_row_source()

    h1, h2 = cursor_src.health(), row_src.health()
    print(f"游标源 {cursor_src.name:<4} health={h1}")
    print(f"行源   {row_src.name:<4} health={h2}")
    if not h1.get("ok") or not h2.get("ok"):
        print("[FAIL] 有源不可用，先修 source 再对账")
        return 2

    try:
        keys = row_src.keys(TABLE, KEY_COLUMN)
    except Exception as exc:
        print(f"[FAIL] 行源枚举失败：{exc}")
        return 2

    bad = []
    checked = 0
    for key in keys:
        pg = query_device_profile(key)
        if pg.get("state") != "found":
            bad.append((key, "存在性", "游标源查无", "行源列了这台"))
            continue
        row = row_src.get_by_key(TABLE, key, KEY_COLUMN)
        if row is None:
            bad.append((key, "存在性", "游标源有", "行源无"))
            continue
        for col in SHARED_COLUMNS:
            checked += 1
            if col in DATE_COLUMNS:
                a, b = norm_date(pg.get(col)), norm_date(row.get(col))
            else:
                a, b = norm(pg.get(col)), norm(row.get(col))
            if a != b:
                bad.append((key, col, a, b))

    print()
    bad_keys = {item[0] for item in bad}
    for key in keys:
        mark = "有差异" if key in bad_keys else "一致"
        print(f"  {key}  {mark}")

    print()
    print(f"比对：{len(keys)} 台 / {checked} 个字段")
    print(f"跳过：{', '.join(CURSOR_ONLY_COLUMNS)}（行源未声明，来自 defect_logs 聚合，不参与判定）")

    if bad:
        print(f"\n[FAIL] {len(bad)} 处不一致：")
        for item in bad:
            print("  ", item)
        return 1

    print("\n[OK] 0 处不一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
