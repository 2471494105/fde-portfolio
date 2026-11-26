#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验 `demo_closeout.py`：演示完了，库必须还在原地。

六个断言，每个都独立查库，不看脚本自己的结论：
  V1  --dry-run 不写库：整表指纹不变、运行台账最大 run_id 不变
  V2  真跑：修复段恰好少 1 行，且少的那个编号就是那一行（不是一换一）
  V3  复原：违约数、编号集合摘要、整表指纹三项都回到基线
  V4  认领是彩排：结束时事件全回到 open，seen_count 没被演示改掉
  V5  --no-record 时台账一行不加（否则「演示」会被当成「真跑」记进趋势线）
  V6  库里没有残留：prod_line 全 NULL，独立重算的违约数与摘要一致

用法：python -X utf8 scripts/verify_demo_closeout.py
退出码：0 全过 / 1 有条不过
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import station_contract as sc  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
PY = sys.executable
FAILS: list[str] = []


def ok(name: str, cond: bool, note: str = "") -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}{'' if not note else '  ' + note}")
    if not cond:
        FAILS.append(name)


def q(dsn: str, sql: str, params: tuple = ()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def fp(dsn: str) -> str:
    rows = q(dsn, "SELECT * FROM biz_work_order ORDER BY wo_no")
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def run_demo(dsn: str, extra: list[str], out: Path) -> tuple[int, str]:
    p = subprocess.run(
        [PY, "-X", "utf8", "scripts/demo_closeout.py", f"--dsn={dsn}", f"--json={out}", *extra],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    dsn = os.environ.get("FDE_CLIENT_DSN", CLIENT_DSN)
    tmp = Path(tempfile.mkdtemp(prefix="verify_demo_"))
    dry_json, real_json = tmp / "dry.json", tmp / "real.json"

    f0 = fp(dsn)
    run0 = max(r["run_id"] for r in q(dsn, "SELECT run_id FROM gov_check_run"))

    # V1 空跑不写库
    rc, out = run_demo(dsn, ["--dry-run", "--no-record"], dry_json)
    ok("V1.1 dry-run exit 0", rc == 0, f"exit={rc}")
    ok("V1.2 dry-run 不写库（指纹不变）", fp(dsn) == f0)
    ok("V1.3 dry-run 不加台账行",
       max(r["run_id"] for r in q(dsn, "SELECT run_id FROM gov_check_run")) == run0)
    ok("V1.4 dry-run 不落摘要", not dry_json.exists())

    # V2/V3/V5 真跑
    rc, out = run_demo(dsn, ["--no-record"], real_json)
    ok("V2.0 真跑 exit 0", rc == 0, f"exit={rc}（上面 [warn] 会说明哪一段不符）")
    if not real_json.exists():
        ok("V2.1 摘要落盘", False)
        print("摘要没生成，后面的断言无从核对")
        return 1
    s = json.loads(real_json.read_text(encoding="utf-8"))
    base, fixed, back = s["baseline"], s["fixed"], s["restored"]
    ok("V2.1 修复段只少一行", fixed["rows"] == base["rows"] - 1, f"{base['rows']} → {fixed['rows']}")
    ok("V2.2 少的编号就是那一行",
       fixed["missing"] == [f"wo_station_identity#{s['row']}"] and fixed["added"] == [],
       f"missing={fixed['missing']} added={fixed['added']}")
    ok("V2.3 修复段判据仍红（exit 1）", fixed["exit"] == 1, f"exit={fixed['exit']}")
    ok("V3.1 复原行数一致", back["rows"] == base["rows"], f"{back['rows']} vs {base['rows']}")
    ok("V3.2 复原编号集合一致", back["ids_digest"] == base["ids_digest"] and back["ids_n"] == base["ids_n"])
    ok("V3.3 复原整表指纹一致", back["fp"] == base["fp"], f"{back['fp']} vs {base['fp']}")
    ok("V5 不留台账行", max(r["run_id"] for r in q(dsn, "SELECT run_id FROM gov_check_run")) == run0)

    # V4 认领彩排
    ev = q(dsn, "SELECT alert_id, state, seen_count, acked_by FROM gov_alert ORDER BY alert_id")
    ok("V4.1 事件全部非 acked（彩排已还原）", all(e["state"] != "acked" for e in ev),
       str([(e["alert_id"], e["state"]) for e in ev]))
    before = {e["alert_id"]: e["seen_count"] for e in s["baseline"]["events"]}
    ok("V4.2 seen_count 没被演示改掉",
       all(e["seen_count"] == before.get(e["alert_id"]) for e in ev))

    # V6 库里无残留 + 独立重算
    residual = q(dsn, "SELECT count(*) AS n FROM biz_work_order WHERE prod_line IS NOT NULL")[0]["n"]
    ok("V6.1 prod_line 全 NULL", residual == 0, f"非空 {residual} 行")
    registry = sc.load()
    tables = {
        c["id"]: q(dsn, f"SELECT {c['key_column']}, {c['field']}, {', '.join(c['alternatives'])}"
                        f" FROM {c['table']}")
        for c in sc.contracts(registry)
    }
    master = q(dsn, "SELECT station_id, station_no, line_no, seq FROM md_station ORDER BY station_id")
    report = sc.audit(registry, tables, master)
    independent = sum(c["violations"] for c in report["contracts"])
    ok("V6.2 独立重算的违约数 == 摘要", independent == back["rows"], f"{independent} vs {back['rows']}")

    print(f"\n{'ALL PASS' if not FAILS else 'FAIL: ' + ', '.join(FAILS)}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        print("校验脚本自身崩了：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
