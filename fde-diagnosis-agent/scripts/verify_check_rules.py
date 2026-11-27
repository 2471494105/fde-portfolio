"""验「判据规矩登记」（check_rules.json）：覆盖、三态、数字拆分、页面口径。

R0 读得到：mock-mes/api/rules.py 能按路径读到 fde 侧的登记（读不到就抛，不兜底）
R1 字段齐：三态合法；assumed 必须 counts_in_total=false 且带 basis/effect/source；
   registered 必须带 decided_by/decided_at（两样缺一样就是「假登记」）
R2 覆盖巡检：patrol.CHECKS 里每一道都登记了（新加判据忘了登记，在这里拦下来）
R3 覆盖台账：gov_check_run 里出现过的 check_id 都登记了
R4 数字拆分：用各判据最新 red_count 复算——确定行 5 / 待确认 402 / 总数 407（2026-11-27 快照）
R5 接口：/v1/obs/runs 给每行带上 rule_status，且 station_contract 不计入、defect_fields 计入
R6 明细页：/v1/problem/1（工位）rule=assumed；/v1/problem/2（缺陷）rule=hard
R7 页面文案：console.html 用「确定违约行」且提到「待确认」；problem.html 有未确认横幅

退出码：0 全 PASS / 1 有 FAIL / 2 接口拿不到（先起 8021）
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
RULES_JSON = ROOT / "data" / "mapping" / "check_rules.json"
SPA = REPO / "mock-mes" / "api" / "static"
BASE = "http://127.0.0.1:8021"
DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(REPO / "mock-mes" / "api"))


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    fails: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('：' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    # ---- R0 读得到（走 mock-mes 的加载路径，不是直接读文件）----
    try:
        import rules  # mock-mes/api/rules.py
        spec = rules.load()
        ok_r0 = rules.status_of("station_contract")["status"] == "assumed"
        check(ok_r0, "R0 页面侧加载器读得到登记，station_contract=assumed",
              f"{len(spec)} 道 · {rules.RULES_PATH.name}")
    except Exception as e:  # noqa: BLE001
        check(False, "R0 页面侧加载器读得到登记", f"{type(e).__name__}: {e}")
        print(f"\n[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 1

    # ---- R1 字段齐 ----
    raw = json.loads(RULES_JSON.read_text(encoding="utf-8"))
    checks = raw["checks"]
    allowed = set(raw["statuses"])
    bad_status = [c["check_id"] for c in checks if c.get("status") not in allowed]
    check(not bad_status, "R1a 三态合法（hard/assumed/registered）", f"非法：{bad_status}")

    bad_assumed = [
        c["check_id"] for c in checks if c.get("status") == "assumed" and (
            c.get("counts_in_total") is not False
            or not c.get("basis") or not c.get("effect") or not c.get("source")
        )
    ]
    check(not bad_assumed, "R1b assumed 必须不计入且带 basis/effect/source", f"缺：{bad_assumed}")

    bad_registered = [
        c["check_id"] for c in checks if c.get("status") == "registered" and (
            not c.get("decided_by") or not c.get("decided_at")
        )
    ]
    check(not bad_registered, "R1c registered 必须带 decided_by + decided_at", f"缺：{bad_registered}")

    # 单位不是「行」的判据不许进行数 KPI（否则 407 会变成 409，这是换单位不是加严）
    bad_kpi = [c["check_id"] for c in checks
               if c.get("row_kpi") and c.get("unit") != "rows"]
    check(not bad_kpi, "R1d row_kpi 只能是 unit=rows 的判据", f"越界：{bad_kpi}")

    # ---- R2 覆盖巡检 ----
    from patrol import CHECKS  # patrol 顶部只有 stdlib，可以安全导入
    ids: set[str] = set()
    for cid, _title, _script in CHECKS:
        ids.update(cid.split("+"))  # data_contract+source_values 拆成两条算
    missing = sorted(i for i in ids if i not in spec)
    check(not missing, "R2 巡检里每道判据都登记了", f"未登记：{missing}")

    # ---- R3 覆盖台账 ----
    import psycopg

    with psycopg.connect(DSN) as conn:
        db_ids = [r[0] for r in conn.execute("select distinct check_id from gov_check_run").fetchall()]
        latest = conn.execute(
            "select check_id, red_count from ("
            "  select check_id, red_count, row_number() over (partition by check_id order by ran_at desc) rn"
            "  from gov_check_run) t where rn = 1"
        ).fetchall()
    missing_db = sorted(i for i in db_ids if i not in spec)
    check(not missing_db, "R3 台账里出现过的 check_id 都登记了", f"未登记：{missing_db}")

    # ---- R4 数字拆分（只算 row_kpi 的判据，与页面同一套名单）----
    by = dict(latest)
    kpi_ids = [cid for cid in by if (spec.get(cid) or {}).get("row_kpi")]
    confirmed = sum(by[cid] for cid in kpi_ids if spec[cid]["counts_in_total"])
    pending = sum(by[cid] for cid in kpi_ids if not spec[cid]["counts_in_total"])
    total = confirmed + pending
    check(confirmed == 5, "R4a 确定违约行 = 5（缺陷字段 5 + 状态 0）", f"confirmed={confirmed}")
    check(pending == 402, "R4b 待口径行 = 402（工位 362 + 报警 40）", f"pending={pending}")
    check(total == 407, "R4c 总数仍是 407（拆分不改变事实，只改变说法）", f"total={total}")

    # ---- R5 接口 ----
    try:
        runs = get("/v1/obs/runs?limit=40")
    except (urllib.error.URLError, ConnectionError) as e:
        check(False, "R5 接口 /v1/obs/runs 可达", f"{type(e).__name__}: {e}（先起 api/run.ps1）")
        print(f"\n[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 2
    recent = runs["recent"]
    have_rule = all("rule_status" in r for r in recent)
    check(have_rule, "R5a 每行都带 rule_status", f"{len(recent)} 行")
    sc = next((r for r in recent if r["check_id"] == "station_contract"), None)
    df = next((r for r in recent if r["check_id"] == "defect_fields"), None)
    check(bool(sc) and sc["counts_in_total"] is False, "R5b 工位判据不计入确定违约",
          f"station_contract={sc and sc.get('counts_in_total')}")
    check(bool(df) and df["counts_in_total"] is True, "R5c 缺陷字段判据计入确定违约",
          f"defect_fields={df and df.get('counts_in_total')}")

    # ---- R6 明细页接口 ----
    try:
        p1 = get("/v1/problem/1")
        p2 = get("/v1/problem/2")
        check(p1["rule"]["status"] == "assumed", "R6a 工位明细 rule=assumed",
              f"status={p1['rule']['status']}")
        check(p2["rule"]["status"] == "hard", "R6b 缺陷字段明细 rule=hard",
              f"status={p2['rule']['status']}")
        check("effect" in p1["rule"] and p1["rule"]["effect"],
              "R6c 未确认的那一类要带「答后去向」", f"effect={str(p1['rule'].get('effect'))[:40]}")
    except (urllib.error.URLError, ConnectionError) as e:
        check(False, "R6 明细接口可达", f"{type(e).__name__}: {e}")

    # ---- R7 页面文案 ----
    console = (SPA / "console.html").read_text(encoding="utf-8")
    problem = (SPA / "problem.html").read_text(encoding="utf-8")
    check("确定违约行" in console, "R7a 中控台 KPI 改叫「确定违约行」")
    check("待确认" in console and "rowsPending" in console, "R7b 中控台把待确认单列成另一个数")
    check("待确认" in problem and ".warn" in problem, "R7c 明细页有未确认横幅")
    # 页面不许再存一份「哪些判据算行类」的名单：那会变成第二个源
    check("ROW_CHECKS" not in console, "R7d 中控台不再硬编码行类判据名单（row_kpi 单一源）")

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 1
    print("[PASS] 判据规矩登记 7 组全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
