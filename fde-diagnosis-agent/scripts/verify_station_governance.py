"""G4–G6 自证：契约能卡住、缺口有人认、单子不许纸面关闭。

七段，全部真库真数，跑完库回到原样（probe schema 已清）：
  S1 前态：工单 60/60、缺陷 302/302 违约；缺口认领中（黄）
  S2 开单：两张整改单，重复跑是 update 不是新建
  S3 纸面关闭：还剩 60 / 302 行，close 必须被拒
  S4 补 1 行：给 WO-2026-0001 补 prod_line='产线A' → 违约 59，该单解析为 S-A01；跑完撤回
  S5 probe 闭环：3 行小表补全 → close 成功，关闭证据写入
  S6 到期反例：--today=2026-12-10 时，认领中的缺口转红（不许「暂时」变永久）
  S7 收尾：真库回到 60/302，probe schema 已清

用法：python scripts/verify_station_governance.py
"""

from __future__ import annotations

import sys
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.ds import station_contract as sc  # noqa: E402
import check_station_contract as csc  # noqa: E402
import raise_remediation as rr  # noqa: E402

DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
TODAY = date.today()
PROBE = "probe_station"


def log(step: str, text: str) -> None:
    print(f"[{step}] {text}")


def audit(registry: dict | None = None, today: date = TODAY) -> dict:
    reg = registry or sc.load()
    tables, master = csc.load_tables(reg, DSN)
    return sc.audit(reg, tables, master, today=today)


def counts(rep: dict) -> dict[str, int]:
    return {c["id"]: c["violations"] for c in rep["contracts"]}


def probe_registry(base: dict) -> dict:
    reg = {
        **base,
        "contracts": [{**base["contracts"][0], "table": f"{PROBE}.wo", "due": "2026-12-03"}],
        "field_gaps": [base["field_gaps"][0]],
    }
    return reg


def main() -> int:
    failures: list[str] = []

    # ---- S1 前态
    rep = audit()
    c = counts(rep)
    log("S1", f"违约 工单 {c['wo_station_identity']}/60  缺陷 {c['defect_station_identity']}/302")
    for g in rep["gaps"]:
        log("S1", f"缺口 {g['id']} = {g['state']}（{g['note']}）")
    if c["wo_station_identity"] != 60 or c["defect_station_identity"] != 302:
        failures.append(f"S1 期望 60/302，实际 {c}")

    # ---- S2 开单（幂等）
    first = rr.raise_orders(DSN, TODAY)
    second = rr.raise_orders(DSN, TODAY)
    log("S2", f"首跑：{[(e['action'], e['rm_no']) for e in first]}")
    log("S2", f"再跑：{[(e['action'], e['rm_no']) for e in second]}")
    if any(e["action"] == "created" for e in second):
        failures.append("S2 重复跑又新建了单子：不幂等")

    # ---- S3 纸面关闭必须被拒
    refused = rr.close_orders(DSN, TODAY)
    log("S3", f"close → {[(e['rm_no'], e['action'], e.get('why')) for e in refused]}")
    if any(e["action"] == "closed" for e in refused):
        failures.append("S3 还有违约行却关闭成功：纸面能关数据问题")

    # ---- S4 补 1 行 → 59，且该单能解析出 S-A01
    try:
        with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("UPDATE biz_work_order SET prod_line = '产线A' WHERE wo_no = 'WO-2026-0001'")
        rep1 = audit()
        c1 = counts(rep1)
        maps = sc.station_maps(csc.load_tables(sc.load(), DSN)[1])
        one = {"prod_line": "产线A", "station_no": "1号工位", "station_id": None}
        sid, _ = sc.resolve(one, sc.contracts(sc.load())[0], maps)
        log("S4", f"补 1 行后违约 {c1['wo_station_identity']}/60；该行解析为 {sid}")
        if c1["wo_station_identity"] != 59 or sid != "S-A01":
            failures.append(f"S4 期望 59 与 S-A01，实际 {c1['wo_station_identity']} 与 {sid}")
    finally:
        with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("UPDATE biz_work_order SET prod_line = NULL WHERE wo_no = 'WO-2026-0001'")

    # ---- S5 probe 闭环
    try:
        with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {PROBE} CASCADE")
            cur.execute(f"CREATE SCHEMA {PROBE}")
            cur.execute(
                f"CREATE TABLE {PROBE}.wo (wo_no text PRIMARY KEY, station_no text NOT NULL,"
                " station_id text, prod_line text)"
            )
            cur.execute(
                f"INSERT INTO {PROBE}.wo VALUES ('P-1', '1号工位', NULL, NULL),"
                " ('P-2', '2号工位', NULL, NULL), ('P-3', '1号工位', 'S-B01', NULL)"
            )
            cur.execute(f"CREATE TABLE {PROBE}.gov_remediation_order (LIKE public.gov_remediation_order INCLUDING ALL)")

        reg = probe_registry(sc.load())
        old = rr.ORDERS_TABLE
        rr.ORDERS_TABLE = f"{PROBE}.gov_remediation_order"
        rp = audit(reg)
        log("S5", f"probe 3 行违约 {rp['contracts'][0]['violations']}：{rp['contracts'][0]['reasons']}")
        created = rr.raise_orders(DSN, TODAY, registry=reg)
        log("S5", f"开单 {[(e['action'], e['rm_no']) for e in created]}")
        refused5 = rr.close_orders(DSN, TODAY, registry=reg)
        log("S5", f"未修时 close → {[(e['action'], e.get('why')) for e in refused5]}")
        with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {PROBE}.wo SET station_id = 'S-A01' WHERE wo_no IN ('P-1','P-2')")
        rp2 = audit(reg)
        closed5 = rr.close_orders(DSN, TODAY, registry=reg)
        log("S5", f"补全后违约 {rp2['contracts'][0]['violations']}；close → {[(e['action'], e['rm_no']) for e in closed5]}")
        with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT rm_no, state, close_evidence FROM {PROBE}.gov_remediation_order")
            rows = [dict(r) for r in cur.fetchall()]
        log("S5", f"单子落库：{[(r['rm_no'], r['state']) for r in rows]}；证据：{rows[0]['close_evidence'] if rows else None}")
        if rp2["contracts"][0]["violations"] != 0 or not rows or rows[0]["state"] != "closed" or not rows[0]["close_evidence"]:
            failures.append("S5 probe 闭环没走通")
        rr.ORDERS_TABLE = old
    finally:
        with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {PROBE} CASCADE")
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (PROBE,)
            )
            gone = cur.fetchone() is None
        log("S5", f"probe schema 已清：{gone}")

    # ---- S6 到期反例
    late = audit(today=date(2026, 12, 10))
    states = {g["id"]: g["state"] for g in late["gaps"]}
    log("S6", f"以 2026-12-10 复跑，缺口状态 {states}")
    if not all(s == "red" for s in states.values()):
        failures.append(f"S6 到期后缺口没转红：{states}")

    # ---- S7 收尾：真库回原样，并让整改单上的数字与现场一致
    rep7 = audit()
    c7 = counts(rep7)
    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM biz_work_order WHERE prod_line IS NOT NULL")
        n_line = cur.fetchone()["n"]
    rr.raise_orders(DSN, TODAY)          # 刷新单子上的违约行数：存下来的数字也要跟着现场走
    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT rm_no, contract_id, violation_count FROM public.gov_remediation_order WHERE state = 'open' ORDER BY rm_no")
        orders = [dict(r) for r in cur.fetchall()]
    log("S7", f"真库回到 工单 {c7['wo_station_identity']}/60、缺陷 {c7['defect_station_identity']}/302，prod_line 非空 {n_line} 行")
    log("S7", f"整改单数字已刷新：{[(o['rm_no'], o['violation_count']) for o in orders]}")
    if c7["wo_station_identity"] != 60 or c7["defect_station_identity"] != 302 or n_line != 0:
        failures.append("S7 真库没回到原样")
    if [(o["violation_count"]) for o in orders] != [60, 302] and [o["violation_count"] for o in orders] != [302, 60]:
        failures.append(f"S7 整改单上的违约行数与现场不一致：{[(o['rm_no'], o['violation_count']) for o in orders]}")

    print()
    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        return 1
    print("[ALL OK] G4–G6 七段全过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        print("自证脚本崩了：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
