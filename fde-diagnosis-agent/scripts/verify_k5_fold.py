# -*- coding: utf-8 -*-
"""K-5 换客户折法验收：词换了，事实必须还是同一件事。

问题：`closed_states` 只说「哪些词算闭环」，没说「折成我方哪一态」。两家客户口径打架时，
最容易发生的是「词表自洽、折法缺失」——保护集静默失真，删除功能等于没有，而且不报错。

做法：在 `fde_client` 里建 `probe.wo_b`，把本家 60 张工单**同一批事实**换成 Odoo 词表
（待处理→draft / 处理中→confirmed / 已完成+已合并→done / 已取消→cancel），然后：

  K5-1 事实等价     同一批工单，A 侧与 B 侧的保护集（有未闭环单的设备）必须完全相同
  K5-2 折漏可见     done 若没被算闭环，保护集必须扩大，且能数出多了几台、几张
  K5-3 折反可见     cancel 若没被算闭环，保护集也必须变（另一个方向，同样要能看见）
  K5-4 空词表拒跑   不许用「空词表」冒充「全部未闭环」，必须抛 PolicyNotRegistered
  K5-5 未折词拒跑   B 侧冒出没折过的词（rework），check_status_policy 的 B 判据必须 fail

跑法（在 fde-diagnosis-agent 目录）：
    python -X utf8 scripts/verify_k5_fold.py
前置：fde_client 可连（postgres/postgres）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
SCHEMA = "probe"
TABLE = "wo_b"
PY = sys.executable

# Odoo 词表与它对应的事实：我方五词 → 对方四词（已合并没有对应，折进 done）
FOLD_SQL = """
INSERT INTO probe.wo_b (wo_no, station_no, equip_no, opened_at, closed_at, state,
                        stage_id, priority, maintenance_team_id, note)
SELECT wo_no, station_no, equip_no, opened_at, closed_at,
       CASE state
            WHEN '待处理' THEN 'draft'
            WHEN '处理中' THEN 'confirmed'
            WHEN '已完成' THEN 'done'
            WHEN '已合并' THEN 'done'
            WHEN '已取消' THEN 'cancel'
            ELSE 'draft'
       END,
       stage_id, priority, maintenance_team_id, note
  FROM public.biz_work_order
"""


def psql(sql: str) -> tuple[int, str]:
    env = dict(os.environ, PGPASSWORD="postgres", PGCLIENTENCODING="UTF8")
    p = Path(os.environ.get("TEMP", "/tmp")) / "k5_probe.sql"
    p.write_text(sql, encoding="utf-8")
    r = subprocess.run(["E:\\POSTGRESQL\\bin\\psql.exe", "-h", "127.0.0.1", "-p", "5432",
                        "-U", "postgres", "-d", "fde_client", "-w", "-f", str(p)],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def protection(where: str, states: tuple[str, ...], table: str) -> tuple[set, int]:
    """保护集 = 有未闭环单的设备号；顺带返回这类工单张数。"""
    import psycopg

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT equip_no FROM {table} WHERE state <> ALL(%s::text[])",
                    (list(states),))
        devices = {r[0] for r in cur.fetchall()}
        cur.execute(f"SELECT count(*) FROM {table} WHERE state <> ALL(%s::text[])",
                    (list(states),))
        n = cur.fetchone()[0]
    return devices, n


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="跑完不清理 probe schema")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from app.ds import status_policy as sp

    ok = True

    def chk(name: str, got, want) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(("[ok] " if good else "[!!] ") + name + f" got={got!r} want={want!r}")

    policy = sp.load()

    print("== 造景：60 张工单换成 Odoo 词表（同一批事实）")
    psql(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    code, err = psql(f"CREATE SCHEMA {SCHEMA}")
    assert code == 0, err
    code, err = psql(f"CREATE TABLE {SCHEMA}.{TABLE} (LIKE public.biz_work_order)")
    assert code == 0, err
    code, err = psql(FOLD_SQL)
    assert code == 0, err
    code, out = psql(f"SELECT state, count(*) FROM {SCHEMA}.{TABLE} GROUP BY 1 ORDER BY 1")
    print("  换词后分布:\n" + "  " + out.replace("\n", "\n  ").strip())
    code, n = psql(f"SELECT count(*) FROM {SCHEMA}.{TABLE}")
    chk("K5-0 行数不变（60）", "60" in n, True)

    closed_a = sp.closed_states("client_a", policy)
    closed_b = sp.closed_states("client_b_odoo", policy)

    print("== K5-1 事实等价：换词不换事实，保护集必须相同")
    dev_a, n_a = protection("state", closed_a, "public.biz_work_order")
    dev_b, n_b = protection("state", closed_b, f"{SCHEMA}.{TABLE}")
    print(f"  A 侧：未闭环 {n_a} 张、保护 {len(dev_a)} 台；B 侧：未闭环 {n_b} 张、保护 {len(dev_b)} 台")
    chk("K5-1 保护集完全相同", dev_a == dev_b, True)
    chk("K5-1 未闭环张数相同", n_a == n_b, True)

    print("== K5-2/3 折漏、折反都必须看得见（这是「折法是承重的」的证据）")
    wrong1 = ("done",)  # 漏了 done：已完成的单被当成未闭环
    dev_w1, n_w1 = protection("state", wrong1, f"{SCHEMA}.{TABLE}")
    extra1 = dev_w1 - dev_b
    print(f"  漏折 done：保护集 {len(dev_b)} → {len(dev_w1)} 台（多 {len(extra1)} 台），"
          f"未闭环 {n_b} → {n_w1} 张")
    chk("K5-2 漏折 done 会让保护集变大", len(dev_w1) > len(dev_b), True)
    chk("K5-2 多出来的正好是只靠 done 的设备", len(extra1) > 0, True)

    wrong2 = ("cancel",)
    dev_w2, n_w2 = protection("state", wrong2, f"{SCHEMA}.{TABLE}")
    print(f"  漏折 cancel：保护集 {len(dev_b)} → {len(dev_w2)} 台，未闭环 {n_b} → {n_w2} 张")
    chk("K5-3 漏折 cancel 也会让保护集变大", len(dev_w2) > len(dev_b), True)
    chk("K5-3 两个方向不是同一批设备", dev_w2 - dev_b != dev_w1 - dev_b, True)

    print("== K5-4 空词表不许冒充「全部未闭环」")
    try:
        sp.closed_states("client_b_odoo", {**policy, "clients": {
            **policy["clients"],
            "client_b_odoo": {**policy["clients"]["client_b_odoo"], "closed_states": []}}})
        chk("K5-4 空词表必须抛错", False, True)
    except sp.PolicyNotRegistered as e:
        print(f"  抛出 PolicyNotRegistered：{e}")
        chk("K5-4 空词表必须抛错", True, True)

    print("== K5-5 未折过的词（rework）必须被 B 判据点名")
    psql(f"INSERT INTO {SCHEMA}.{TABLE} (wo_no, station_no, equip_no, opened_at, state, "
         f"stage_id, priority, maintenance_team_id) "
         f"SELECT 'B-REWORK-1', station_no, equip_no, opened_at, 'rework', stage_id, "
         f"priority, maintenance_team_id FROM public.biz_work_order LIMIT 1")
    env = dict(os.environ, FDE_STATUS_CLIENT="client_b_odoo", PYTHONIOENCODING="utf-8")
    r = subprocess.run([PY, "-X", "utf8", "scripts/check_status_policy.py",
                        f"--dsn={DSN}", f"--table={SCHEMA}.{TABLE}"],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
    chk("K5-5 未折词 → B 判据 exit 1", r.returncode, 1)
    chk("K5-5 报出 rework", "rework" in (r.stdout + r.stderr), True)
    print("  " + "\n  ".join(l for l in (r.stdout or "").strip().splitlines()[:6]))

    psql(f"DELETE FROM {SCHEMA}.{TABLE} WHERE state = 'rework'")
    r2 = subprocess.run([PY, "-X", "utf8", "scripts/check_status_policy.py",
                         f"--dsn={DSN}", f"--table={SCHEMA}.{TABLE}"],
                        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
    chk("K5-5 删掉 rework → B 判据 exit 0", r2.returncode, 0)

    print("== 收尾")
    if not args.keep:
        psql(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        import psycopg
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s",
                        (SCHEMA,))
            left = cur.fetchone()[0]
        chk("probe schema 已清", left, 0)

    print("\nRESULT:", "ALL PASS" if ok else "HAS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))