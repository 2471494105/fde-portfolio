"""测数据库自己的底线：哪些脏行它已经挡了，哪些挡不住。

产出是断言清单的依据——DB 挡住的，断言不必重复写一遍；挡不住的，才是
数据断言真正要管的事。不做这一步，断言清单就是拍脑袋，而且会写一堆
永远不可能失败的断言（那等于没有断言）。

用法：
  python scripts/check_db_floor.py                  # 默认 fde_sandbox
  python scripts/check_db_floor.py --dsn=...         # 换库

只允许在沙箱库跑：每条用例都在事务里执行并回滚，不会写脏任何一张表。
主库跑也不脏，但会被误读成"主库能塞脏数据"，所以默认写死 sandbox。
"""

import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"

# (场景说明, SQL, 期望)
# 期望 block = 业务上明显是脏的，预期 DB 应该拒绝
# 期望 pass  = 业务上脏，但预期 DB 拦不住（断言必须自己管）
CASES = [
    (
        "verdict 写成小写 ok",
        "INSERT INTO defect_logs (ts, device_id, station, defect_type, confidence, batch, verdict, is_actually_ok)"
        " VALUES ('2026-11-05 08:00:00','CX-101','1-3号工位','划伤',0.9,'B20261105','ok',false)",
        "block",
    ),
    (
        "日志引用不存在的设备 CX-999",
        "INSERT INTO defect_logs (ts, device_id, station, defect_type, confidence, batch, verdict, is_actually_ok)"
        " VALUES ('2026-11-05 08:00:00','CX-999','1-3号工位','划伤',0.9,'B20261105','NG',false)",
        "block",
    ),
    (
        "告警严重级别写成 fatal",
        "INSERT INTO alarms (code, meaning, common_cause, severity)"
        " VALUES ('E9999','x','y','fatal')",
        "block",
    ),
    (
        "标定结论写成「合格」",
        "INSERT INTO calibrations (device_id, ts, drift_px, drift_mm, result, operator)"
        " VALUES ('CX-101','2026-11-05 08:00:00',0.1,0.1,'合格','张三')",
        "block",
    ),
    (
        "批次号为 NULL",
        "INSERT INTO defect_logs (ts, device_id, station, defect_type, confidence, batch, verdict, is_actually_ok)"
        " VALUES ('2026-11-05 08:00:00','CX-101','1-3号工位','划伤',0.9,NULL,'NG',false)",
        "block",
    ),
    (
        "同一幂等键再建一张待处理单",
        "INSERT INTO work_orders (order_id, device_id, root_cause, root_cause_code, severity, actions,"
        " evidence_refs, idempotency_key, status, assignee_group)"
        " VALUES ('WO-SB-999','CX-101','x','light','low','x','[]','k-101-light','待处理','设备科')",
        "block",
    ),
    (
        "设备号为空字符串",
        "INSERT INTO devices (device_id, line, station, camera_model, lens_model, light_model,"
        " install_date, maintenance_cycle_days) VALUES ('','3号线','1-3号工位','X','16mm','CA-DC21E','2025-01-01',90)",
        "pass",
    ),
    (
        "保养周期为负数",
        "UPDATE devices SET maintenance_cycle_days = -5 WHERE device_id = 'CX-101'",
        "pass",
    ),
    (
        "置信度 999",
        "INSERT INTO defect_logs (ts, device_id, station, defect_type, confidence, batch, verdict, is_actually_ok)"
        " VALUES ('2026-11-05 08:00:00','CX-101','1-3号工位','划伤',999,'B20261105','NG',false)",
        "pass",
    ),
    (
        "光源累计时长超过标称寿命 4 倍",
        "UPDATE light_usage SET cumulative_hours = 33000 WHERE device_id = 'CX-101'",
        "pass",
    ),
    (
        "工单指向不存在的设备",
        "INSERT INTO work_orders (order_id, device_id, root_cause, root_cause_code, severity, actions,"
        " evidence_refs, idempotency_key, status, assignee_group)"
        " VALUES ('WO-SB-998','CX-999','x','other','low','x','[]','k999','待处理','设备科')",
        "pass",
    ),
    (
        "告警记录时间在 2099 年",
        "INSERT INTO alarm_events (ts, device_id, code) VALUES ('2099-01-01 00:00:00','CX-101','E1001')",
        "pass",
    ),
    (
        "日志工位与设备档案不一致",
        "INSERT INTO defect_logs (ts, device_id, station, defect_type, confidence, batch, verdict, is_actually_ok)"
        " VALUES ('2026-11-05 08:00:00','CX-104','3号线','划伤',0.9,'B20261105','NG',false)",
        "pass",
    ),
    (
        "标定漂移像素与毫米符号相反",
        "INSERT INTO calibrations (device_id, ts, drift_px, drift_mm, result, operator)"
        " VALUES ('CX-101','2026-11-05 08:00:00',0.2,-0.2,'通过','张三')",
        "pass",
    ),
]


def main() -> int:
    dsn = DEFAULT_DSN
    for arg in sys.argv[1:]:
        if arg.startswith("--dsn="):
            dsn = arg.split("=", 1)[1]
    if "fde_rag" in dsn:
        print("[FAIL] 拒绝在 fde_rag 上跑：这个脚本会插脏数据（虽然会回滚）")
        return 2

    print(f"目标库：{dsn.rsplit('/', 1)[-1]}")
    print()
    print(f"{'#':>2}  {'场景':<34} {'DB 反应':<22} {'结论'}")
    print("-" * 88)

    blocked = passed = 0
    mismatched = []

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for idx, (name, sql, expect) in enumerate(CASES, 1):
                try:
                    with conn.transaction():
                        cur.execute(sql)
                        raise RuntimeError("__would_pass__")
                except RuntimeError as exc:
                    if "__would_pass__" not in str(exc):
                        raise
                    verdict = "通过（没拦住）"
                    db_says = "pass"
                except Exception as exc:
                    verdict = f"被拦（{type(exc).__name__}）"
                    db_says = "block"

                if db_says == "block":
                    blocked += 1
                else:
                    passed += 1

                tail = "断言不必重复" if db_says == "block" else "断言必须自己管"
                print(f"{idx:>2}  {name:<34} {verdict:<22} {tail}")
                if db_says != expect:
                    mismatched.append((idx, name, expect, db_says))

    print("-" * 88)
    print(f"DB 拦住 {blocked} 条，拦不住 {passed} 条，共 {len(CASES)} 条")
    if mismatched:
        print(f"\n[注意] {len(mismatched)} 条与预期不符：")
        for idx, name, expect, actual in mismatched:
            print(f"  #{idx} {name}：预期 {expect}，实际 {actual}")
        return 1
    print("\n[OK] 全部与预期一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
