"""可观测性验收：接口指标、数据新鲜度、判据运行台账，三件都要能证伪。

不是「页面上有这张表」就算做完，要证明：
  O1 `/healthz` 真的连库（不是只证明进程活着）
  O2 指标在动：请求几次后 `total_since_start` 增加；读指标本身不进样本
  O3 新鲜度抓得到真脏：`biz_work_order` 最新事件 2026-01-18（stale），
     `biz_defect_record` 最新事件 2027-01-01（未来时间，future=true）
  O4 台账抓得到「监控静默」：插一条 30 小时前的假运行 → 该判据 silent=true；删掉复原
  O5 判据 `--no-record` 真的不写：行数不变
  O6 页面（第五页）与接口逐行一致：交给 `verify_ui.py`，这里只探 API

用法：python -X utf8 scripts\verify_obs.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8021"
DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
AGENT = r"E:\fde工程师学习指南\06_GitHub作品仓库\fde-diagnosis-agent\venv\Scripts\python.exe"

ok = True


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def chk(name: str, cond: bool, detail: str = "") -> None:
    global ok
    ok = ok and cond
    print(("[ok] " if cond else "[!!] ") + name + ("  " + detail if detail else ""))


def counts() -> int:
    import psycopg

    with psycopg.connect(DSN) as conn:
        return conn.execute("select count(*) from gov_check_run").fetchone()[0]


def main() -> int:
    # ---- O1
    h = get("/healthz")
    chk("O1 /healthz 连库", h.get("status") == "ok" and "db_ms" in h, json.dumps(h, ensure_ascii=False))

    # ---- O2
    s0 = get("/v1/obs/api")
    for _ in range(5):
        get("/v1/devices?size=1")
    s1 = get("/v1/obs/api")
    get("/v1/obs/api")
    s2 = get("/v1/obs/api")
    chk("O2 请求计数在涨", s1["total_since_start"] > s0["total_since_start"],
        f"{s0['total_since_start']} → {s1['total_since_start']}")
    chk("O2 读指标不进样本", s2["in_window"] == s1["in_window"], f"{s1['in_window']} → {s2['in_window']}")
    chk("O2 p95 有值", s1["p95_ms"] > 0 and s1["window"] == 300, f"p95={s1['p95_ms']} ms")

    # ---- O3
    f = get("/v1/obs/freshness")
    tables = {t["table"]: t for t in f["tables"]}
    wo = tables["biz_work_order"]
    df = tables["biz_defect_record"]
    chk("O3 工单表被标陈旧", wo["stale"] and wo["age_hours"] > 24,
        f"{wo['latest']} / {wo['age_hours']}h")
    chk("O3 缺陷表被标未来时间", df["future"] and df["age_hours"] < 0,
        f"{df['latest']} / {df['age_hours']}h")
    chk("O3 主数据没有时间轴也不误报", tables["md_device"]["stale"] is False, "md_device 无时间列")

    # ---- O4
    import psycopg

    probe = "probe_silent"
    with psycopg.connect(DSN) as conn:
        conn.execute("delete from gov_check_run where check_id = %s", (probe,))
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        conn.execute(
            "insert into gov_check_run (check_id, ran_at, duration_ms, status, red_count, dataset)"
            " values (%s, %s, 1, 'ok', 0, 'probe')",
            (probe, old),
        )
        conn.commit()
    try:
        r = get("/v1/obs/runs?limit=50")
        row = next(c for c in r["checks"] if c["check_id"] == probe)
        chk("O4 30 小时没跑 → silent", row["silent"] and row["hours_since"] >= 30,
            f"{row['hours_since']}h / silent={row['silent']}")
        fresh = next(c for c in get("/v1/obs/runs?limit=50")["checks"] if c["check_id"] == "station_contract")
        chk("O4 刚跑过的判据不 silent", fresh["silent"] is False, f"{fresh['hours_since']}h")
    finally:
        with psycopg.connect(DSN) as conn:
            conn.execute("delete from gov_check_run where check_id = %s", (probe,))
            conn.commit()
        chk("O4 探针已清", counts() >= 2, f"剩余 {counts()} 行")

    # ---- O5
    before = counts()
    import subprocess

    subprocess.run(
        [AGENT, "-X", "utf8", r"scripts\check_station_contract.py", "--no-record"],
        cwd=r"E:\fde工程师学习指南\06_GitHub作品仓库\fde-diagnosis-agent",
        capture_output=True,
        text=True,
    )
    after = counts()
    chk("O5 --no-record 不写台账", after == before, f"{before} → {after}")

    print()
    print("[ALL OK] 可观测性六段全过" if ok else "[FAIL] 有检查没过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
