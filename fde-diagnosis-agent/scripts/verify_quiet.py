"""验 C3 静默窗口：窗口内不推、窗口外照推、补推不丢、配置错要拦住。

Q1 不设窗口：行为跟以前一模一样（没有「静默」字样，台账 quiet 为空）
Q2 窗口内没有待推事件时：明确报「在窗口内」，不假装推过
Q3 窗口内**有**待推事件（用一条被打上 deferred 的真事件当探针）：压住不推、事件上留 deferred、台账记窗口
Q4 窗口外同一条件：真的尝试外发（死端口 → 失败留证），deferred 被清掉——证明静默不是丢消息
Q5 配置写错（`2200`）：exit 2 并说「配置错」，不许当「今天没事」

探针使用事件 #1（`station_contract:red`）的 `detail.delivery`，跑完把 detail 原样写回。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
SCRIPT = ROOT / "scripts" / "raise_alerts.py"
DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
DEAD = "http://127.0.0.1:9/nope"  # 必然连不上，用来分辨「想推但失败」与「压根没推」
PROBE_KEY = "station_contract:red"


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run([PY, "-X", "utf8", str(SCRIPT), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def detail_of(key: str) -> dict:
    with conn() as c:
        return c.execute("select detail from gov_alert where dedup_key=%s", (key,)).fetchone()["detail"] or {}


def last_sweep() -> dict:
    with conn() as c:
        return c.execute("select details from gov_check_run where check_id='alert_sweep'"
                         " order by run_id desc limit 1").fetchone()["details"] or {}


def set_detail(key: str, detail: dict) -> None:
    with conn() as c:
        c.execute("update gov_alert set detail=%s::jsonb where dedup_key=%s",
                  (json.dumps(detail, ensure_ascii=False), key))
        c.commit()


def stamp(hh: int, mm: int) -> str:
    return datetime(2026, 11, 27, hh, mm).astimezone().isoformat()


def main() -> int:
    fails: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('：' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    saved = detail_of(PROBE_KEY)
    try:
        # Q1 不设窗口：行为不变
        code, out = run("--quiet-off", f"--now={stamp(23, 30)}", "--no-notify")
        check("静默窗口" not in out and last_sweep().get("quiet") is None,
              "Q1 不设窗口时行为与以前一致（无静默字样、台账 quiet 为空）",
              f"exit={code}")

        # Q2 窗口内、无待推事件
        code, out = run(f"--quiet=22:00-08:00", f"--now={stamp(23, 30)}", "--no-notify")
        check("现在在窗口内" in out and last_sweep().get("quiet") == "22:00-08:00",
              "Q2 窗口内明确报「在窗口内」，台账记下窗口",
              f"exit={code} quiet={last_sweep().get('quiet')}")

        # Q3 窗口内带待推事件（探针：打上 deferred）
        set_detail(PROBE_KEY, {**saved, "delivery": {"deferred": "22:00-08:00", "ok": False}})
        code, out = run("--quiet=22:00-08:00", f"--now={stamp(23, 30)}", f"--webhook={DEAD}")
        dl = (detail_of(PROBE_KEY).get("delivery") or {})
        check("留在投递箱" in out and "外发失败" not in out,
              "Q3a 窗口内压住不推（有「留在投递箱」，无「外发失败」）", out.strip()[-160:])
        check(dl.get("deferred") == "22:00-08:00",
              "Q3b 事件上留 deferred（下一轮才补得出来）", json.dumps(dl, ensure_ascii=False))

        # Q4 窗口外：真推，失败留证，deferred 清掉
        code, out = run("--quiet-off", f"--now={stamp(10, 0)}", f"--webhook={DEAD}")
        dl = (detail_of(PROBE_KEY).get("delivery") or {})
        check("外发失败" in out and code == 2,
              "Q4a 窗口外真的尝试外发（死端口 → 失败且 exit 2）", f"exit={code}")
        check(dl.get("deferred") is None and dl.get("ok") is False,
              "Q4b 补推过一次后 deferred 清掉、失败留证（静默不等于丢消息）",
              json.dumps(dl, ensure_ascii=False))

        # Q5 配置写错
        code, out = run(f"--quiet=2200", f"--now={stamp(23, 30)}", "--no-notify")
        check(code == 2 and "配置错" in out, "Q5 窗口写错 exit 2（不当「今天没事」）",
              f"exit={code}")
        code, out = run("--quiet=22.00-08.00", f"--now={stamp(23, 30)}", "--no-notify")
        check(code == 2 and "配置错" in out, "Q5b `22.00` 这种宽容写法也拒绝", f"exit={code}")
    finally:
        set_detail(PROBE_KEY, saved)
        print("（探针 detail 已写回原样）")

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 1
    print("[OK] C3 静默窗口全部通过（不设不变 · 窗口内压住留痕 · 窗口外补推 · 配置错拦住）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(2)
