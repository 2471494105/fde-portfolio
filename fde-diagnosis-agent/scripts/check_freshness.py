#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""判据 F：数据新鲜度（每张业务表「最近一条」离现在多久）。

为什么这条判据以前不存在
------------------------
页面早就显示「陈旧 4 表 / 未来 1 表」，但那是 KPI：看得见、不响。根因不是判据难写，
而是「表-时间列」这份清单只活在 `mock-mes/api/obs.py` 里。判据侧再抄一份，就是
DOC-60 记过的「同一条规则写四遍」——两套口径各自走，页面绿、判据红，谁也说不清谁对。

所以这一版先把清单抽成 `data/mapping/freshness_map.json`，页面与判据**读同一份**，
然后在清单里给每张表标 `static`：

- `static: true`：本仓是 2026-11 的静态种子，24 小时阈值必然判陈旧——那是样本身份，
  不是客户数据漂了。这类表只进观测、不判红。
- `static: false`：只有 `gov_check_run`（判据自己多久没跑）。它是元观测，停摆才是真事。

判定两条：
  F1 陈旧：非 static 表，`now - max(time_col) > max_age_hours`
  F2 未来：有 time_col 且 max > now（未来水位会让增量静默停摆，DOC-40 踩过）

**故意不事件化**：这条判据现在只落台账、不进 `gov_issue_catalog` 事件流。
理由写在 DOC-72 第七节：一开事件，静态样本就会造出一条永久红（4 张表），
把「9 类里 3 类真红」的口径污染成「4 条事件」，演示数字（违约行 407）也要跟着重算。
等真客户数据接进来、`static` 摘掉之后，再把 `freshness` 加进 `alerts.TITLES` 即可。

用法：
    python -X utf8 scripts/check_freshness.py
    python -X utf8 scripts/check_freshness.py --no-record
    python -X utf8 scripts/check_freshness.py --dsn=... --map=...

退出码：0 无红 / 1 有红 / 2 自身出错（不能当「今天没事」）
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import runlog  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
MAP_PATH = ROOT / "data" / "mapping" / "freshness_map.json"
CHECK_ID = "freshness"


def load_map(path: Path = MAP_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def probe(dsn: str, spec: dict) -> tuple[list[dict], datetime]:
    """逐表取行数与最近时间；时间列不存在就记为自身错误，不静默跳过。"""
    import psycopg
    from psycopg.rows import dict_row

    now = datetime.now(timezone.utc).astimezone()
    out: list[dict] = []
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for t in spec["tables"]:
            table, tcol = t["table"], t.get("time_col")
            rows = conn.execute("SELECT count(*) AS n FROM " + table).fetchone()["n"]
            latest = None
            if tcol:
                latest = conn.execute("SELECT max(" + tcol + ") AS t FROM " + table).fetchone()["t"]
            age_h = None
            if latest is not None:
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                age_h = round((now - latest).total_seconds() / 3600.0, 1)
            out.append({
                "table": table, "time_col": tcol, "kind": t.get("kind"),
                "static": bool(t.get("static")), "rows": rows,
                "latest": latest.isoformat(sep=" ") if latest else None,
                "age_hours": age_h,
                "is_future": age_h is not None and age_h < 0,
            })
    return out, now


def judge(rows: list[dict], max_age: float) -> dict:
    stale, future = [], []
    for r in rows:
        if not r["time_col"]:
            continue
        if r["static"]:
            continue  # 静态样本不判陈旧/未来：那是样本身份
        if r["is_future"]:
            future.append(r)
        elif r["age_hours"] is not None and r["age_hours"] > max_age:
            stale.append(r)
    ids = [f"fresh_stale:{r['table']}" for r in stale] + [f"fresh_future:{r['table']}" for r in future]
    problems = (["%s：%s 已 %s 小时没有新数据（阈值 %s）" % (r["table"], r["time_col"], r["age_hours"], max_age)
                 for r in stale]
                + ["%s：%s 最新一条在未来（%s），增量水位会跳到未来" % (r["table"], r["time_col"], r["latest"])
                   for r in future])
    return {"stale": [r["table"] for r in stale], "future": [r["table"] for r in future],
            "ids": ids, "problems": problems}


def main(argv: list[str]) -> int:
    dsn, map_path, record_run = CLIENT_DSN, MAP_PATH, True
    for a in argv:
        if a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--map="):
            map_path = Path(a.split("=", 1)[1])
        elif a == "--no-record":
            record_run = False
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    t0 = time.perf_counter()
    try:
        spec = load_map(map_path)
        rows, now = probe(dsn, spec)
    except Exception as e:  # noqa: BLE001
        print(f"[error] 取数失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    max_age = float(spec.get("default_max_age_hours", 24))
    res = judge(rows, max_age)
    print(f"观测面 {len(rows)} 张表 @ {dsn.rsplit('/', 1)[-1]}，阈值 {max_age} 小时，判定时刻 {now:%Y-%m-%d %H:%M}")
    for r in rows:
        tag = "静态跳过" if r["static"] else ("未来" if r["is_future"] else
                                           ("陈旧" if (r["age_hours"] or 0) > max_age else "ok"))
        print("  %-22s %-12s %-8s 行=%-5s 最近=%s（%s 小时前）" % (
            r["table"], r["time_col"] or "-", tag, r["rows"], r["latest"] or "-", r["age_hours"]))

    red = len(res["ids"])
    status = "fail" if red else "ok"
    print(f"\n红 {red} 项（陈旧 {len(res['stale'])} · 未来 {len(res['future'])}）")
    for p in res["problems"]:
        print("  - " + p)

    if record_run:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        details = {
            "red_metric": "stale_or_future_tables",
            "ids": res["ids"],
            "stale": res["stale"],
            "future": res["future"],
            "static_skipped": [r["table"] for r in rows if r["static"] and r["time_col"]],
            "threshold_hours": max_age,
            "map": str(map_path.name),
        }
        try:
            run_id = runlog.record(CHECK_ID, status, red, duration_ms,
                                   dataset=f"fde_client 表级新鲜度（{len(rows)} 张表）",
                                   details=details, dsn=dsn)
            print(f"运行台账 → run_id={run_id}（{status}，红 {red} 项，{duration_ms} ms）")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 运行台账没写进去：{type(e).__name__}: {e}", file=sys.stderr)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        print("检查脚本自身崩了（这一轮结果不可当结论）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
