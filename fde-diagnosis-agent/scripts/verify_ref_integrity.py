"""验判据 M（跨表引用一致性）：真红、正向对照、不比对有原因、参数与台账行为。

R1 真红：客户库里真有 1 行指向不存在的设备（缺陷 `rec_id=9001` → `CX-999`）
R2 正向对照：维修单 → 缺陷的引用 0 孤儿（证明不是见谁都红）
R3 「不比对」清单带原因：两套叫法的字段必须写清为什么跳过，不是静默不查
R4 未知参数 exit 2、--no-record 不写台账、正常跑写 1 行
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
SCRIPT = ROOT / "scripts" / "check_ref_integrity.py"
DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"


def run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run([PY, "-X", "utf8", str(SCRIPT), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def rows_in_log(check_id: str) -> int:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as c:
        return c.execute("select count(*) from gov_check_run where check_id = %s",
                         (check_id,)).fetchone()[0]


def main(argv: list[str]) -> int:
    fails: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('：' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "ref.json"
        code, out = run(["--no-record", f"--json={js}"])
        if not js.exists():
            check(False, "R0 能输出 json", out[-300:])
            print()
            print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
            return 1
        res = json.loads(js.read_text(encoding="utf-8"))

    by = {r["name"]: r for r in res["refs"]}

    # R1 真红
    r1 = by.get("defect_equip", {})
    check(code == 1, "R1a 有孤儿时 exit 1（红是结论，不是崩溃）", f"exit={code}")
    check(r1.get("orphans") == 1 and res["ids"] == ["defect_equip#9001"],
          "R1b 缺陷 → 设备：1 行孤儿，编号对得上",
          f"orphans={r1.get('orphans')} ids={res['ids']}")

    # R2 正向对照：不是见谁都红
    r2 = by.get("repair_defect", {})
    check(r2.get("orphans") == 0 and r2.get("rows", 0) > 0,
          "R2 维修单 → 缺陷：0 孤儿（正向对照）", f"rows={r2.get('rows')}")

    # R3 不比对必须有原因
    nc = res.get("not_compared") or []
    check(len(nc) == 2 and all(n["pair"] and n["why"] for n in nc),
          "R3 「不比对」两条都带原因", " / ".join(n["pair"] for n in nc))

    # R4 参数与台账
    code_bad, _ = run(["--bogus"])
    check(code_bad == 2, "R4a 未知参数 exit 2", f"exit={code_bad}")
    before = rows_in_log("ref_integrity")
    run(["--no-record"])
    after_nr = rows_in_log("ref_integrity")
    check(after_nr == before, "R4b --no-record 不写台账", f"{before} → {after_nr}")
    code_rec, _ = run([])
    after = rows_in_log("ref_integrity")
    check(after == before + 1 and code_rec == 1, "R4c 正常跑写 1 行台账（红时 exit 1）",
          f"{after_nr} → {after}, exit={code_rec}")

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 1
    print("[OK] 判据 M 全部通过（真红 1 行 · 正向对照 0 · 跳过有原因 · 台账行为对）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(2)
