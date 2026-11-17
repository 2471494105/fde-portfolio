"""把 work_orders 的幂等键迁到「设备 + 故障分类，不含日期」，并换成部分唯一索引。

背景
----
旧键是 sha256(device_id|day|root_cause_code)，带日期。后果：

    同一个坏光源（CX-101 光源超额定寿命）连着三天，
    每天各建一张待处理单 —— WO-20261115-001 / WO-20261116-002 / WO-20261117-007。
    三张单同时"待处理"，设备科不知道该处理哪张，而数据库认为三张都合法。

新键去掉 day。为了让"处理完再坏还能建新单"，唯一约束从
「同一个键全表只能有一条」改成「同一个键同时只能有一条未关闭的单」：

    CREATE UNIQUE INDEX ... ON work_orders (idempotency_key) WHERE status <> '已完成';

用法
----
    python scripts/migrate_idem_key.py           # 只预览，不写库
    python scripts/migrate_idem_key.py --apply   # 真执行

幂等：重复跑是安全的。已迁移过的项会被跳过。
"""
from __future__ import annotations

import argparse
import hashlib
import sys

import psycopg
from psycopg.rows import dict_row

DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"

# 历史单没有 root_cause_code 列，只能按 root_cause 全文人工归属。
# 这是一次性的对照表，新单由工具层传枚举值，不靠猜。
LEGACY_CODE = {
    "WO-20261115-001": "light_degradation",
    "WO-20261116-002": "light_degradation",
    "WO-20261116-003": "threshold_misconfig",
    "WO-20261116-004": "calibration_drift",
    "WO-20261116-005": "other",           # 算法端模型漂移，枚举里没有对应项
    "WO-20261116-006": "batch_drift",
    "WO-20261117-007": "light_degradation",
}


def idem_key(device_id: str, root_cause_code: str) -> str:
    """与 mock_qms/server.py 的 _idem_key 保持一字不差。"""
    raw = f"{device_id}|{root_cause_code}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库；默认只预览")
    args = ap.parse_args()

    conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("select column_name from information_schema.columns "
                        "where table_name='work_orders'")
            cols = {r["column_name"] for r in cur.fetchall()}
            has_code = "root_cause_code" in cols

            cur.execute("select indexname, indexdef from pg_indexes where tablename='work_orders'")
            idx = {r["indexname"]: r["indexdef"] for r in cur.fetchall()}

            sel = "order_id, device_id, root_cause, idempotency_key, status, created_at"
            if has_code:
                sel = ("order_id, device_id, root_cause, root_cause_code, "
                       "idempotency_key, status, created_at")
            cur.execute(f"select {sel} from work_orders order by created_at")
            rows = cur.fetchall()

        print(f"库内 {len(rows)} 张单；root_cause_code 列：{'有' if has_code else '无'}")
        print(f"索引：{sorted(idx)}\n")

        # ---------- 第 1 步：拆掉旧的「全表唯一」 ----------
        old_uniques = [k for k in idx if k == "work_orders_idempotency_key_key"]
        print("① 拆旧的全表唯一约束：", old_uniques or "（已不存在，跳过）")

        # ---------- 第 2 步：补 root_cause_code 列 ----------
        print(f"② 补列 root_cause_code：{'（已存在，跳过）' if has_code else 'ADD COLUMN'}")

        # ---------- 第 3 步：给历史单归属分类码 ----------
        unmapped = [r["order_id"] for r in rows if r["order_id"] not in LEGACY_CODE]
        if unmapped:
            # 不猜：没有对照表的单先停下，让人补
            print(f"!! 有 {len(unmapped)} 张单不在对照表里，需要人工补：{unmapped}", file=sys.stderr)
            return 2
        print("③ 历史单分类码归属：")
        for r in rows:
            code = LEGACY_CODE[r["order_id"]]
            old = r["root_cause_code"] if has_code else "(无此列)"
            print(f"   {r['order_id']}  {r['device_id']}  {code}"
                  f"{'' if old == code else f'   <- 原值 {old}'}")

        # ---------- 第 4 步：重算键 ----------
        planned = []
        for r in rows:
            code = LEGACY_CODE[r["order_id"]]
            newk = idem_key(r["device_id"], code)
            planned.append((r, code, newk))
        print("\n④ 重算幂等键（前 8 位对比）：")
        for r, code, newk in planned:
            changed = "" if r["idempotency_key"][:8] == newk[:8] else "  *变化*"
            print(f"   {r['order_id']}  {r['idempotency_key'][:8]} -> {newk[:8]}{changed}")

        # ---------- 第 5 步：同键多条未关闭，只留最早 ----------
        from collections import defaultdict
        groups = defaultdict(list)
        for r, code, newk in planned:
            if r["status"] != "已完成":
                groups[newk].append(r)
        collisions = {k: v for k, v in groups.items() if len(v) > 1}
        print(f"\n⑤ 同键多条未关闭（需合并）：{len(collisions)} 组")
        keep = {}
        drop = []
        for k, items in collisions.items():
            items_sorted = sorted(items, key=lambda x: x["created_at"])
            keep[k] = items_sorted[0]["order_id"]
            for r in items_sorted[1:]:
                drop.append(r["order_id"])
            print(f"   key {k[:8]}：保留 {items_sorted[0]['order_id']}，"
                  f"标'已合并' -> {[r['order_id'] for r in items_sorted[1:]]}")

        if not args.apply:
            print("\n（预览模式，未写库。加 --apply 执行）")
            return 0

        with conn.cursor() as cur:
            if "work_orders_idempotency_key_key" in idx:
                cur.execute("ALTER TABLE work_orders "
                            "DROP CONSTRAINT IF EXISTS work_orders_idempotency_key_key")
            if not has_code:
                cur.execute("ALTER TABLE work_orders "
                            "ADD COLUMN IF NOT EXISTS root_cause_code TEXT NOT NULL DEFAULT 'other'")

            for r, code, newk in planned:
                cur.execute("UPDATE work_orders SET root_cause_code = %s, idempotency_key = %s "
                            "WHERE order_id = %s", (code, newk, r["order_id"]))

            for oid in drop:
                cur.execute("UPDATE work_orders SET status = '已合并' WHERE order_id = %s", (oid,))

            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS work_orders_open_key "
                        "ON work_orders (idempotency_key) "
                        "WHERE status NOT IN ('已完成', '已合并', '已取消')")
        conn.commit()
        print("\n已提交。索引现在是：")
        with conn.cursor() as cur:
            cur.execute("select indexname, indexdef from pg_indexes where tablename='work_orders'")
            for r in cur.fetchall():
                print("  ", r["indexname"], "->", r["indexdef"])

        print("\n迁移后：")
        with conn.cursor() as cur:
            cur.execute("select order_id, device_id, root_cause_code, "
                        "substring(idempotency_key,1,8) as k, status from work_orders "
                        "order by created_at")
            for r in cur.fetchall():
                print("  ", r["order_id"], "|", r["device_id"], "|", r["root_cause_code"],
                      "|", r["k"], "|", r["status"])
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
