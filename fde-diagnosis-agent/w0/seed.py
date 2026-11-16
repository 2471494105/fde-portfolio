"""W0 最小数据集：3 台视觉检测设备 + 光源使用 + 误判日志（内存生成，不落库）。"""

from datetime import datetime, timedelta
import random

DEVICES = {
    "CX-101": dict(
        device_id="CX-101", line="2号线", station="1号工位",
        camera_model="CV-2000M", lens_model="LM-16F", light_model="LF-200",
        install_date="2023-06-15", maintenance_cycle_days=90,
    ),
    "CX-102": dict(
        device_id="CX-102", line="3号线", station="2号工位",
        camera_model="CV-2000M", lens_model="LM-25F", light_model="LF-300",
        install_date="2024-03-20", maintenance_cycle_days=90,
    ),
    "CX-103": dict(
        device_id="CX-103", line="3号线", station="3号工位",
        camera_model="CV-1200C", lens_model="LM-16F", light_model="LF-300",
        install_date="2024-11-08", maintenance_cycle_days=90,
    ),
}

# CX-101 光源累计时长已超额定寿命，且亮度被人为从 80 下调到 65 —— H1 的关键证据
LIGHT_USAGE = {
    "CX-101": dict(
        light_model="LF-200", cumulative_hours=8620, rated_life_hours=8000,
        brightness_set=65,
        brightness_history=[
            {"ts": "2025-03-10", "value": 80},
            {"ts": "2026-09-02", "value": 65},
        ],
        last_replaced_at="2023-06-15",
        degradation_estimate="偏高：累计时长已超额定寿命",
    ),
    "CX-102": dict(
        light_model="LF-300", cumulative_hours=3240, rated_life_hours=10000,
        brightness_set=80,
        brightness_history=[{"ts": "2024-03-20", "value": 80}],
        last_replaced_at="2024-03-20",
        degradation_estimate="正常",
    ),
    "CX-103": dict(
        light_model="LF-300", cumulative_hours=4180, rated_life_hours=10000,
        brightness_set=80,
        brightness_history=[{"ts": "2024-11-08", "value": 80}],
        last_replaced_at="2024-11-08",
        degradation_estimate="正常",
    ),
}

DEFECT_TYPES = ["划痕", "凹坑", "尺寸超差", "污渍"]


def _build_logs():
    """造三组形态不同的日志，让聚合结果可区分。"""
    rng = random.Random(42)
    logs = []
    t0 = datetime(2026, 11, 1, 8, 0)

    # CX-101：误判全部集中在 1 号工位，且夜班占比明显偏高 —— 指向硬件侧
    for i in range(40):
        ts = t0 + timedelta(hours=2 * i)
        night = ts.hour >= 20 or ts.hour < 4
        if i < 26:
            conf = round(rng.uniform(0.55, 0.78), 3)   # 良品被判不良
            is_ok = True
            # 让夜班误判更密集
            night = True if (i % 10) < 7 else night
        else:
            conf = round(rng.uniform(0.88, 0.98), 3)
            is_ok = False
        logs.append(dict(
            device_id="CX-101", ts=ts.isoformat(sep=" "), line="2号线", station="1号工位",
            defect_type=rng.choice(DEFECT_TYPES), confidence=conf,
            batch="B20261101" if i % 4 == 0 else "B20261102",
            is_actually_ok=is_ok, night_shift=night,
        ))

    # CX-102：误判随机分布在各工位，但集中在同一物料批次 —— 指向算法/数据侧
    for i in range(40):
        ts = t0 + timedelta(hours=2 * i)
        if i < 22:
            conf = round(rng.uniform(0.55, 0.79), 3)
            is_ok = True
        else:
            conf = round(rng.uniform(0.88, 0.98), 3)
            is_ok = False
        logs.append(dict(
            device_id="CX-102", ts=ts.isoformat(sep=" "), line="3号线",
            station=rng.choice(["2号工位", "3号工位", "4号工位"]),
            defect_type=rng.choice(DEFECT_TYPES), confidence=conf,
            # 前面误判集中在新供应商批次
            batch="B20261105" if (i < 22 and i % 10 < 8) else rng.choice(["B20261103", "B20261106"]),
            is_actually_ok=is_ok, night_shift=False,
        ))

    # CX-103：只有"划痕"这一类反复被判错 —— 指向阈值/模型偏置
    for i in range(40):
        ts = t0 + timedelta(hours=2 * i)
        if i < 15:
            conf = round(rng.uniform(0.60, 0.79), 3)
            is_ok = True
            dt = "划痕" if i % 10 < 9 else rng.choice(["凹坑", "污渍"])
        else:
            conf = round(rng.uniform(0.88, 0.98), 3)
            is_ok = False
            dt = rng.choice(DEFECT_TYPES)
        logs.append(dict(
            device_id="CX-103", ts=ts.isoformat(sep=" "), line="3号线", station="3号工位",
            defect_type=dt, confidence=conf,
            batch="B20261104",
            is_actually_ok=is_ok, night_shift=False,
        ))

    return logs


LOGS = _build_logs()
