"""生成 W1 主库种子数据 → data/seed/002_data.sql

设计目标：让 5 类故障各有**可区分的统计特征**，能被 query_defect_log 的聚合查出来。

    设备      故障类型      证据特征
    CX-101   光源衰减      误判集中 1 号工位 + 夜班密集；光源 8620h 超额定 8000h；亮度被两次下调
    CX-102   新物料批次    误判集中 batch B20261105
    CX-103   单一缺陷阈值  误判集中 defect_type=划痕
    CX-104   标定漂移      11-08 之后误判陡增，与 calibration 的 drift_mm 超阈值吻合
    CX-105   镜头污染      误判率随时间单调上升，标定正常、无告警

数据说明：型号参考 Cognex In-Sight 2800 系列、Keyence CV-X400 系列公开规格；
告警码为仿真实格式自编（E-xxx），非厂商官方文档原文。
"""

import random
from datetime import datetime, timedelta
from pathlib import Path

RNG = random.Random(42)
START = datetime(2026, 11, 1, 0, 0, 0)
DAYS = 14

DEVICES = [
    # device_id, line, station, camera, lens, light, install, cycle
    ("CX-101", "3号线", "1-3号工位", "In-Sight 2801", "16mm", "CA-DC21E", "2025-03-12", 90),
    ("CX-102", "3号线", "1-3号工位", "In-Sight 2802", "16mm", "CA-DC21E", "2025-05-20", 90),
    ("CX-103", "3号线", "1-3号工位", "CV-X420 + CA-H200CX", "16mm", "CA-DC21E", "2024-11-08", 90),
    ("CX-104", "4号线", "1-2号工位", "CV-X450 + CA-H200CX", "16mm", "CA-DC21E", "2024-08-15", 90),
    ("CX-105", "4号线", "1-2号工位", "In-Sight 2800", "16mm", "CA-DC21E", "2025-09-01", 90),
]

STATIONS = {  # 每台设备覆盖的工位
    "CX-101": ["1号工位", "2号工位", "3号工位"],
    "CX-102": ["1号工位", "2号工位", "3号工位"],
    "CX-103": ["1号工位", "2号工位", "3号工位"],
    "CX-104": ["1号工位", "2号工位"],
    "CX-105": ["1号工位", "2号工位"],
}

DEFECT_TYPES = ["划痕", "凹坑", "尺寸超差", "污渍"]
BATCHES = [f"B2026{1100 + i}" for i in range(10)]  # B20261100 ~ B20261109
BAD_BATCH = "B20261105"  # CX-102 的问题批次

# 告警码字典（仿真实格式自编）
ALARMS = [
    ("E-101", "相机未连接或通讯中断", "网线松脱、相机掉电", "critical"),
    ("E-102", "触发信号超时", "PLC 触发回路异常、编码器信号丢失", "warning"),
    ("E-201", "光源亮度低于设定值", "光源老化、驱动电流不足", "warning"),
    ("E-202", "光源累计点亮时长超过额定寿命", "光源衰减，需更换", "warning"),
    ("E-301", "标定数据无效或过期", "标定后未保存、标定板偏移", "warning"),
    ("E-401", "图像对比度不足", "镜头污染、光源衰减、工件表面反光", "info"),
    ("W-501", "连续判定 NG 超过阈值", "阈值过严或确实存在批量不良", "info"),
    ("W-502", "误判率超过设定阈值", "需人工复核判定参数", "warning"),
]


def night_weight(hour: int) -> float:
    """夜班（22:00-06:00）误判更明显，模拟光照不足放大光源问题。"""
    return 1.0 if 6 <= hour < 22 else 2.2


def fp_prob(device: str, ts: datetime, station: str, defect_type: str, batch: str) -> float:
    """返回该条被判 NG 但实际良品的概率（over-kill 概率）。"""
    hour = ts.hour
    day_idx = (ts - START).days

    if device == "CX-101":
        # 光源衰减：集中 1 号工位，夜班更严重
        base = 0.52 if station == "1号工位" else 0.07
        return min(0.92, base * night_weight(hour))

    if device == "CX-102":
        # 新物料批次
        return 0.58 if batch == BAD_BATCH else 0.06

    if device == "CX-103":
        # 单一缺陷类型
        return 0.66 if defect_type == "划痕" else 0.05

    if device == "CX-104":
        # 标定漂移：11-08 之后陡增
        return 0.44 if day_idx >= 7 else 0.06

    if device == "CX-105":
        # 镜头污染：随运行时间加速累积（二次曲线，前段慢、后段快）
        t = day_idx / (DAYS - 1)
        return 0.03 + 0.45 * t * t

    return 0.05


def gen_logs(device: str, n: int) -> list:
    rows = []
    stations = STATIONS[device]
    for _ in range(n):
        # 时间：14 天内均匀，夜班段额外加样本（现场夜班产出也高）
        day = RNG.randrange(DAYS)
        if RNG.random() < 0.35:
            hour = RNG.choice([22, 23, 0, 1, 2, 3, 4, 5])
        else:
            hour = RNG.randrange(6, 22)
        ts = START + timedelta(days=day, hours=hour,
                               minutes=RNG.randrange(60), seconds=RNG.randrange(60))

        station = RNG.choice(stations)
        defect_type = RNG.choice(DEFECT_TYPES)
        batch = RNG.choice(BATCHES)

        # 让 CX-102 的问题批次在样本里占比更高（符合"换了一批料"的现场）
        if device == "CX-102" and RNG.random() < 0.3:
            batch = BAD_BATCH
        # 让 CX-103 的划痕样本更多（"只有划痕老判错"说明划痕判得多）
        if device == "CX-103" and RNG.random() < 0.35:
            defect_type = "划痕"

        is_actually_ok = RNG.random() < 0.93  # 绝大多数产品是良品

        p_ng = fp_prob(device, ts, station, defect_type, batch)
        if is_actually_ok:
            # 良品被判 NG 的概率 = 误判概率
            verdict = "NG" if RNG.random() < p_ng else "OK"
            confidence = RNG.uniform(0.70, 0.94) if verdict == "NG" else RNG.uniform(0.90, 0.99)
        else:
            # 真不良被漏检的概率较低
            verdict = "OK" if RNG.random() < 0.08 else "NG"
            confidence = RNG.uniform(0.88, 0.99) if verdict == "NG" else RNG.uniform(0.55, 0.75)

        rows.append((ts, device, station, defect_type, round(confidence, 3),
                     batch, verdict, is_actually_ok))
    rows.sort(key=lambda r: r[0])
    return rows


def sql_str(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def main():
    out = ["BEGIN;",
           "TRUNCATE defect_logs, alarm_events, calibrations, light_brightness_history, "
           "light_usage, work_orders, devices, alarms RESTART IDENTITY CASCADE;"]

    # devices
    out.append("INSERT INTO devices (device_id, line, station, camera_model, lens_model, "
               "light_model, install_date, maintenance_cycle_days) VALUES")
    vals = [f"({','.join([sql_str(x) for x in d])})" for d in DEVICES]
    out.append(",\n".join(vals) + ";")

    # alarms
    out.append("INSERT INTO alarms (code, meaning, common_cause, severity) VALUES")
    vals = [f"({','.join([sql_str(x) for x in a])})" for a in ALARMS]
    out.append(",\n".join(vals) + ";")

    # light_usage
    light_rows = [
        ("CX-101", "CA-DC21E", 8620, 8000, 65, "2024-09-20"),
        ("CX-102", "CA-DC21E", 5120, 8000, 80, "2025-05-20"),
        ("CX-103", "CA-DC21E", 6415, 8000, 78, "2024-11-08"),
        ("CX-104", "CA-DC21E", 4380, 8000, 82, "2024-08-15"),
        ("CX-105", "CA-DC21E", 3960, 8000, 80, "2025-09-01"),
    ]
    out.append("INSERT INTO light_usage (device_id, light_model, cumulative_hours, "
               "rated_life_hours, brightness_set, last_replaced_at) VALUES")
    out.append(",\n".join(f"({','.join([sql_str(x) for x in r])})" for r in light_rows) + ";")

    # brightness 变更历史：CX-101 两次下调（典型的"治标"操作）
    bh = [
        ("CX-101", "2025-03-12 09:00:00", 85, "装机调试设定"),
        ("CX-101", "2026-09-15 14:20:00", 80, "夜班误判升高，下调亮度"),
        ("CX-101", "2026-10-20 10:05:00", 65, "误判持续，再次下调"),
        ("CX-102", "2025-05-20 09:00:00", 80, "装机调试设定"),
        ("CX-103", "2024-11-08 09:00:00", 78, "装机调试设定"),
        ("CX-104", "2024-08-15 09:00:00", 82, "装机调试设定"),
        ("CX-105", "2025-09-01 09:00:00", 80, "装机调试设定"),
    ]
    out.append("INSERT INTO light_brightness_history (device_id, ts, value) VALUES")
    out.append(",\n".join(f"({sql_str(r[0])}, {sql_str(r[1])}, {r[2]})" for r in bh) + ";")

    # 标定记录
    cal_rows = []
    # 正常设备：8 月起约每 18 天一次，漂移在正常范围
    for dev in ["CX-101", "CX-102", "CX-103", "CX-105"]:
        base = datetime(2026, 8, 1)
        for i in range(6):
            ts = base + timedelta(days=i * 18 + RNG.randrange(5), hours=RNG.randrange(9, 17))
            drift_mm = round(RNG.uniform(0.03, 0.16), 3)
            cal_rows.append((dev, ts, round(drift_mm * 5 + RNG.uniform(0, 0.2), 3),
                             drift_mm, "通过", RNG.choice(["张伟", "李强", "王敏"])))

    # CX-104：11-07 标定后漂移超阈值，与 11-08 误判陡增吻合
    for ts, drift_mm in [
        (datetime(2026, 8, 12, 10, 0), 0.05),
        (datetime(2026, 9, 20, 14, 0), 0.08),
        (datetime(2026, 10, 26, 9, 30), 0.11),
        (datetime(2026, 11, 7, 8, 30), 0.42),
    ]:
        result = "告警" if drift_mm > 0.3 else "通过"
        cal_rows.append(("CX-104", ts, round(drift_mm * 5 + 0.15, 3),
                         drift_mm, result, RNG.choice(["张伟", "李强", "王敏"])))
    cal_rows.sort(key=lambda r: (r[0], r[1]))
    out.append("INSERT INTO calibrations (device_id, ts, drift_px, drift_mm, result, operator) VALUES")
    out.append(",\n".join(
        f"({sql_str(r[0])}, {sql_str(r[1])}, {r[2]}, {r[3]}, {sql_str(r[4])}, {sql_str(r[5])})"
        for r in cal_rows) + ";")

    # alarm_events：CX-101 有光源寿命告警，CX-105 无告警（污染不报码）
    ae = [
        ("2026-11-03 02:14:00", "CX-101", "E-202"),
        ("2026-11-09 23:40:00", "CX-101", "E-202"),
        ("2026-11-12 01:05:00", "CX-101", "W-502"),
        ("2026-11-06 15:22:00", "CX-104", "E-301"),
        ("2026-11-08 14:30:00", "CX-104", "E-301"),
    ]
    out.append("INSERT INTO alarm_events (ts, device_id, code) VALUES")
    out.append(",\n".join(f"({sql_str(r[0])}, {sql_str(r[1])}, {sql_str(r[2])})" for r in ae) + ";")

    # defect_logs：每台 200 条
    all_rows = []
    for dev in STATIONS:
        all_rows.extend(gen_logs(dev, 200))
    all_rows.sort(key=lambda r: r[0])

    out.append("INSERT INTO defect_logs (ts, device_id, station, defect_type, confidence, "
               "batch, verdict, is_actually_ok) VALUES")
    out.append(",\n".join(
        "(" + ",".join([sql_str(x) for x in r]) + ")" for r in all_rows) + ";")

    out.append("COMMIT;")

    p = Path(__file__).parent / "seed" / "002_data.sql"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"已生成 {p}")
    print(f"defect_logs: {len(all_rows)} 条 / calibrations: {len(cal_rows)} 条 / alarms: {len(ALARMS)} 条")


if __name__ == "__main__":
    main()
