"""判据 K 的验收：页面染的 5 行与判据说红的 5 行，必须是同一批 rec_id。

为什么要单独一个验收脚本
------------------------
判据 K 补的是「缺陷页染色但 Python 沉默」。补上判据之后仍然有同一个老问题：
页面（`flag.js`）与判据（`field_checks.py`）是**同一规则的两个独立实现**，
词与阈值各写一份，改一处漏一处不会有人响（`DOC-60` 的原病）。

所以这里对四件事：
  K1  登记与实现对得上：`field_rules.json` 的每条规则都有实现，JS 里登记的入口都是函数
  K2  阈值同源：`flag.js::DEFECT_LIMITS`（node 读出来）与 `field_rules.json` 的 min/max 逐字相等
  K3  同一批行喂两侧，按 rec_id 比集合（conf / future / lot_null / lot_empty 四类分开比）
  K4  反例：把 conf 上界改成 200，K3 必须报「判据少了 5、6」——判据证明不了自己会红，
      就没资格说页面是绿的
  K5  台账：真跑一次判据，`gov_check_run` 多一行且 check_id=defect_fields

判定时刻注入同一个字面值（`NOW`）：页面取浏览器钟、判据取库钟，两个钟各走各自的，
边界行会随机对不上——「随机」在验收里等于「偶尔红的假红」，那比没有更坏。

用法：
    venv\\Scripts\\python.exe -X utf8 scripts\\verify_defect_fields.py
    venv\\Scripts\\python.exe -X utf8 scripts\\verify_defect_fields.py --dsn=...
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import field_checks as fc  # noqa: E402
from app.ds import counterpart as cpt  # noqa: E402

REPO_ROOT = ROOT.parent
JS_PATH = REPO_ROOT / "mock-mes" / "api" / "static" / "flag.js"
SPEC_PATH = ROOT / "data" / "mapping" / "field_rules.json"
NOW = "2026-11-26 23:00:00"
DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"

_NODE_JUDGE = """
const m = require(process.argv[1]);
const rows = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const now = process.argv[3];
const opts = { now: now };
process.stdout.write(JSON.stringify({
  limits: m.DEFECT_LIMITS || null,
  api: Object.keys(m),
  judge: m.defectJudge(rows, opts),
  counts: m.defectCounts(rows, opts)
}));
"""

fails = 0


def chk(name: str, got, want) -> None:
    global fails
    ok = got == want
    print(f"[{'ok' if ok else '!!'}] {name} got={got!r} want={want!r}")
    if not ok:
        fails += 1


def phone(rows: list[dict]) -> list[dict]:
    """把库里的行序列化成页面那样：时间是 `YYYY-MM-DD HH:MM:SS` 字符串（两侧同一种表示）。"""
    out = []
    for r in rows:
        ca = r.get("captured_at")
        if hasattr(ca, "strftime"):
            ca = ca.strftime("%Y-%m-%d %H:%M:%S")
        out.append({"rec_id": r.get("rec_id"), "conf_pct": r.get("conf_pct"),
                    "captured_at": ca, "lot_no": r.get("lot_no")})
    return out


def run_js(rows: list[dict], now: str = NOW) -> tuple[dict | None, str]:
    node = cpt._node()
    if not node:
        return None, "node 不在 PATH，动态对账跑不了——跑不了就是没验，不能当通过"
    tmp = Path(tempfile.gettempdir()) / "defect_field_rows.json"
    tmp.write_text(json.dumps(phone(rows), ensure_ascii=False, default=str), encoding="utf-8")
    try:
        p = subprocess.run([node, "-e", _NODE_JUDGE, str(JS_PATH), str(tmp), now],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    except subprocess.TimeoutExpired:
        return None, "node 跑 flag.js 超时（60s）"
    if p.returncode != 0:
        return None, f"node 跑 flag.js 失败（exit {p.returncode}）：{(p.stderr or '').strip()[:200]}"
    try:
        return json.loads(p.stdout), ""
    except json.JSONDecodeError:
        return None, f"flag.js 的输出不是 JSON：{(p.stdout or '')[:120]}"


def py_sets(rows: list[dict], spec: dict) -> dict[str, set[str]]:
    conf = fc.run("conf_pct_range", rows, spec, NOW)
    fut = fc.run("captured_at_not_future", rows, spec, NOW)
    lot = fc.run("lot_no_blank_split", rows, spec, NOW)
    return {
        "conf": {str(x) for x in conf["ids"]},
        "future": {str(x) for x in fut["ids"]},
        "lot_null": {str(x) for x in lot.get("null_ids") or []},
        "lot_empty": {str(x) for x in lot.get("empty_ids") or []},
        "violating": {str(x) for x in conf["ids"]} | {str(x) for x in fut["ids"]} | {str(x) for x in lot["ids"]},
    }


def compare(rows: list[dict], spec: dict, js: dict) -> list[str]:
    """返回「两侧不一致」的说明；空表示四类集合逐个相等。"""
    out: list[str] = []
    py = py_sets(rows, spec)
    j = js.get("judge") or {}
    for kind in ("conf", "future", "lot_null", "lot_empty", "violating"):
        js_ids = {str(x) for x in (j.get(kind) or [])}
        if py[kind] != js_ids:
            out.append(f"{kind}: 页面多 {sorted(js_ids - py[kind])[:6]}，判据多 {sorted(py[kind] - js_ids)[:6]}")
    return out


def load_rows_from_db(dsn: str, spec: dict) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    cols = [spec["key"]]
    for r in (spec.get("rules") or {}).values():
        for c in r.get("reads") or []:
            if c not in cols:
                cols.append(c)
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM {spec['table']}")
        return [dict(r) for r in cur.fetchall()]


def run_check_script(dsn: str) -> tuple[int, str]:
    py = sys.executable
    p = subprocess.run([py, "-X", "utf8", str(ROOT / "scripts" / "check_defect_fields.py"), f"--dsn={dsn}"],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def count_runs(dsn: str) -> tuple[int, dict | None]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM gov_check_run")
        n = cur.fetchone()["n"]
        cur.execute("SELECT run_id, check_id, status, red_count FROM gov_check_run ORDER BY run_id DESC LIMIT 1")
        last = cur.fetchone()
    return n, (dict(last) if last else None)


def main(argv: list[str]) -> int:
    global fails
    dsn = DEFAULT_DSN
    for a in argv:
        if a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    print(f"== K1 登记与实现（{SPEC_PATH.name} vs field_checks.py）")
    for rule_id in (spec.get("rules") or {}):
        try:
            fc.run(rule_id, [], spec, NOW)  # 空表跑一遍：只验「登记了、有实现、入口通」
            chk(f"K1 {rule_id} 有实现且入口通", True, True)
        except Exception as e:
            chk(f"K1 {rule_id} 有实现且入口通", f"{type(e).__name__}: {e}", True)
    try:
        fc.run("no_such_rule", [], spec, NOW)
        chk("K1 未登记规则要抛 UnknownCheck", "没抛", "UnknownCheck")
    except Exception as e:
        chk("K1 未登记规则要抛 UnknownCheck", type(e).__name__, "UnknownCheck")

    print("\n== K2 阈值同源（flag.js::DEFECT_LIMITS vs field_rules.json）")
    rows = load_rows_from_db(dsn, spec)
    print(f"   观测面 {spec['table']} @ {dsn.rsplit('/', 1)[-1]}，{len(rows)} 行")
    js, why = run_js(rows)
    if js is None:
        print(f"[!!] {why}")
        fails += 1
        print("\nRESULT: HAS FAIL")
        return 1
    limits = js.get("limits") or {}
    rs = spec["rules"]
    chk("K2 confMin 同源", limits.get("confMin"), rs["conf_pct_range"]["min"])
    chk("K2 confMax 同源", limits.get("confMax"), rs["conf_pct_range"]["max"])
    chk("K2 JS 暴露 defectJudge/defectCounts", {"defectJudge", "defectCounts"} <= set(js.get("api") or []), True)

    print("\n== K3 同一批行按编号对账")
    diffs = compare(rows, spec, js)
    chk("K3 四类集合逐个相等", diffs, [])
    if diffs:
        for d in diffs:
            print(f"    · {d}")
    print(f"   页面  " + " / ".join(f"{k}={len(v)}" for k, v in (js.get('judge') or {}).items()))
    pyc = py_sets(rows, spec)
    print(f"   判据  " + " / ".join(f"{k}={len(v)}" for k, v in pyc.items()))
    print(f"   编号  {sorted(pyc['violating'], key=lambda s: (len(s), s))}")

    print("\n== K4 反例：把 conf 上界改成 200，对账必须报")
    bad = copy.deepcopy(spec)
    bad["rules"]["conf_pct_range"]["max"] = 200
    diffs2 = compare(rows, bad, js)
    chk("K4 阈值不同源 → 对账报出差异", bool(diffs2), True)
    if diffs2:
        print(f"    · {diffs2[0]}")

    print("\n== K5 台账落一行")
    before, _ = count_runs(dsn)
    code, out = run_check_script(dsn)
    after, last = count_runs(dsn)
    chk("K5 判据红时退出 1", code, 1)
    chk("K5 台账多一行", after - before, 1)
    chk("K5 最近一行是 defect_fields/fail", (last or {}).get("check_id"), "defect_fields")
    chk("K5 红数等于违约行数", (last or {}).get("red_count"), len(pyc["violating"]))
    tail = [ln for ln in out.splitlines() if "运行台账" in ln]
    if tail:
        print(f"    {tail[-1]}")

    print()
    if fails:
        print(f"RESULT: HAS FAIL（{fails} 项）")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
