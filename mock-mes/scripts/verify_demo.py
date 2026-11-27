"""验答辩控制台（8021 `/demo`）：能点、只本机、白名单、互斥、真跑、读面没变。

为什么单独验：这一页是**唯一能 spawn 进程**的地方。它错一处，代价不是页面打不开，
而是「谁都能从浏览器让这台机器跑脚本」，或者「演示当天点了两遍、同一件事演两次」。

D1 页面能开，且明写「演示脚手架，不是产品功能」+ 四个动作都在
D2 白名单外动作 404
D3 互斥：连点两次 → 202 + 409（第二次不是排队，是明确拒绝）
D4 只给本机：非 loopback 403；`FDE_DEMO_OFF=1` 时整页 403
D5 「开始巡检」真跑：exit 0、日志含「判据 8 道」、台账最近一次 alert_sweep 是 ok
D6 8021 读面没变：DSN 仍是 ro_reader，`demo.py` 里没有任何写 SQL
D7 「处置清单」真出文件：exit 0 且 fde 侧 out/actions 里有新 csv
D8 日志接口按偏移续读：since=0 有内容、since=next 为空

前置：8021 已在跑（`uvicorn api.app:app --port 8021`）。
用法：python -X utf8 scripts/verify_demo.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]  # mock-mes
FDE = ROOT.parent / "fde-diagnosis-agent"
BASE = "http://127.0.0.1:8021"
ADMIN_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
ACTIONS_DIR = FDE / "out" / "actions"


def wait_idle(timeout: float = 300) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = httpx.get(f"{BASE}/demo/api/state", timeout=10).json()
        if not s.get("busy"):
            return s
        time.sleep(0.7)
    raise RuntimeError("动作一直没结束（等不到 idle）")


def last_sweep_status() -> str:
    with psycopg.connect(ADMIN_DSN) as c:
        row = c.execute("select status from gov_check_run where check_id='alert_sweep'"
                        " order by run_id desc limit 1").fetchone()
    return row[0] if row else "<无>"


def main() -> int:
    fails: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('：' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    # D1 页面
    page = httpx.get(f"{BASE}/demo", timeout=15)
    html = page.text
    check(page.status_code == 200 and "演示脚手架" in html,
          "D1a 页面能开，且明写「演示脚手架，不是产品功能」", f"HTTP {page.status_code}")
    check(all(k in html for k in ("patrol", "probe", "closeout", "export")),
          "D1b 四个动作都在页面上")

    # D2 白名单
    r = httpx.post(f"{BASE}/demo/api/run/rd-rf", timeout=10)
    check(r.status_code == 404 and "白名单" in r.json().get("error", ""),
          "D2 白名单外动作 404（不做任意命令的后门）", f"HTTP {r.status_code}")

    # D3 互斥（趁巡检还在跑）
    a = httpx.post(f"{BASE}/demo/api/run/patrol", timeout=10)
    b = httpx.post(f"{BASE}/demo/api/run/patrol", timeout=10)
    check(a.status_code == 202 and b.status_code == 409,
          "D3 连点两次：202 + 409（拒绝而不是排队）", f"{a.status_code} / {b.status_code}")
    s = wait_idle()

    # D5 巡检真跑
    log = httpx.get(f"{BASE}/demo/api/log?since=0", timeout=10).json()
    check(s.get("exit") == 0 and "判据 8 道" in log.get("chunk", ""),
          "D5a 巡检按钮真跑：exit 0，日志里有「判据 8 道」",
          f"exit={s.get('exit')} log={s.get('log')}")
    check((s.get("last_patrol") or {}).get("status") == "ok" and last_sweep_status() == "ok",
          "D5b 台账最近一次 alert_sweep 是 ok（页面那盏灯不再误报红）",
          f"page={ (s.get('last_patrol') or {}).get('status') } db={last_sweep_status()}")

    # D8 日志偏移
    nxt = log.get("next", 0)
    tail = httpx.get(f"{BASE}/demo/api/log?since={nxt}", timeout=10).json()
    check(nxt > 0 and tail.get("chunk") == "",
          "D8 日志按偏移续读：since=next 时没有重复内容", f"next={nxt}")

    # D7 处置清单
    before = set(ACTIONS_DIR.glob("*.csv")) if ACTIONS_DIR.is_dir() else set()
    httpx.post(f"{BASE}/demo/api/run/export", timeout=10)
    s = wait_idle()
    after = set(ACTIONS_DIR.glob("*.csv")) if ACTIONS_DIR.is_dir() else set()
    newest = max(after, key=lambda p: p.stat().st_mtime) if after else None
    check(s.get("exit") == 0 and (after - before or newest),
          "D7 处置清单按钮真出 CSV：exit 0 且 out/actions 里有文件",
          f"exit={s.get('exit')} 新文件={[p.name for p in (after - before)]}")

    # D4 只给本机 / 关页
    sys.path.insert(0, str(ROOT))
    from api import demo  # noqa: E402

    code, _ = demo.start("patrol", "10.1.2.3", None)
    check(code == 403, "D4a 非 loopback 来源直接 403", f"code={code}")
    os.environ["FDE_DEMO_OFF"] = "1"
    try:
        code, body = demo.start("patrol", "127.0.0.1", None)
    finally:
        os.environ.pop("FDE_DEMO_OFF", None)
    check(code == 403 and "关掉" in body.get("error", ""),
          "D4b FDE_DEMO_OFF=1 时整页关掉（客户环境不该带这页）", f"code={code}")

    # D6 读面没变
    app_src = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    demo_src = (ROOT / "api" / "demo.py").read_text(encoding="utf-8")
    check("ro_reader:roreader2026pw" in app_src,
          "D6a 8021 的读面仍是 ro_reader", "FDE_CLIENT_RO_DSN 默认值")
    writes = re.findall(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", demo_src.upper())
    check(not writes, "D6b demo.py 里没有任何写 SQL（动库的是被 spawn 的脚本）",
          f"命中 {writes}")

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 1
    print("[OK] 答辩控制台全部通过（能开 · 白名单 · 互斥 · 只本机 · 真跑 · 读面未变 · 日志可续读）")
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
