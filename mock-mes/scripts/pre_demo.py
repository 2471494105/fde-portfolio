#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""答辩前一条命令自检：环境 → 服务 → 巡检 → 五个验收 → 页面口径 → 一句话结论。

为什么单独一份
--------------
现场出问题的时候，人最缺的不是能力，是「先查哪一项」。现有的验收分散在五六个脚本里
（`verify_console` / `verify_obs` / `verify_demo` / `verify_check_rules` / `verify_problem`），
每个都要记路径、记用哪个 venv；`fde-diagnosis-agent/scripts/preflight.py` 是**评测前的**
库与服务自检（8010 / 种子态 / 事实层），不覆盖 8021 这套页面。

这份只回答一个问题：**现在这台机器，能不能上台**。
- 它不改数据，也不替人判断「讲得好不好」；
- 每一项都给「下一个动作」：没过就先做那一句，不要现场 debug 五个脚本。

用法（工作目录 mock-mes）：
    ..\\fde-diagnosis-agent\\venv\\Scripts\\python.exe -X utf8 scripts\\pre_demo.py
    ... --start            # 8021 没起就顺手拉起来（隐藏窗口）
    ... --with-closeout    # 多跑一项收口演示验收（会留两行台账）
    ... --base http://127.0.0.1:8022   # 换端口时只影响本脚本自己的健康检查与页面读取

注意 `--base` 不传导：五个验收脚本各自写死 8021，真换端口要连它们一起改。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]                    # mock-mes
FDE = HERE.parent / "fde-diagnosis-agent"
VENV_PY = FDE / "venv" / "Scripts" / "python.exe"
BASE = "http://127.0.0.1:8021"   # 可被 --base 覆盖（现场换端口/换机时用）

# 五个验收：三个页面侧、两个 fde 侧。名字里带「验什么」，出红时不用回头翻脚本
SUITES = [
    ("页面口径（首屏 3 卡 / 白话 / 题面）", HERE / "scripts" / "verify_console.py", 300),
    ("可观测性六段（观测隔离 / 静默 / 新鲜度）", HERE / "scripts" / "verify_obs.py", 180),
    ("答辩控制台（白名单 / 互斥 / 只本机）", HERE / "scripts" / "verify_demo.py", 180),
    ("判据规矩（5 / 402 / 407 与三态）", FDE / "scripts" / "verify_check_rules.py", 240),
    ("明细链（事件 → 台账编号 → 目标行）", FDE / "scripts" / "verify_problem.py", 180),
]
CLOSEOUT = ("收口演示（补一行 → 归零 → 复原）", FDE / "scripts" / "verify_demo_closeout.py", 240)

OK_MARKS = ("ALL PASS", "ALL OK", "全过", "[OK]", "PASS")


def log(msg: str) -> None:
    print(msg, flush=True)


def http_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def http_text(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read().decode("utf-8")


def start_service() -> bool:
    """8021 没在听就拉起来（隐藏窗口）。返回是否起来了。"""
    if not VENV_PY.exists():
        log(f"  [!] 找不到 venv：{VENV_PY}")
        return False
    log("  … 8021 没在听，按 --start 拉起（隐藏窗口）")
    subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "api.app:app", "--host", "127.0.0.1", "--port", "8021"],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(20):
        time.sleep(0.6)
        try:
            http_json("/healthz")
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def run_suite(name: str, script: Path, timeout: int) -> tuple[bool, str, float]:
    if not script.exists():
        return False, f"脚本不存在：{script}", 0.0
    t0 = time.time()
    try:
        p = subprocess.run(
            [str(VENV_PY if VENV_PY.exists() else sys.executable), "-X", "utf8", str(script)],
            cwd=str(script.parents[1]),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"超时 > {timeout}s（先看服务是否有响应）", time.time() - t0
    out = (p.stdout or "") + (p.returncode and (p.stderr or "") or "")
    ok = p.returncode == 0
    keep = [ln.strip() for ln in out.splitlines()
            if any(m in ln for m in OK_MARKS) or "[FAIL]" in ln]
    tail = keep[-1] if keep else (out.strip().splitlines() or ["（无输出）"])[-1]
    return ok, tail[:110], time.time() - t0


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true", help="8021 没起就顺手拉起来")
    ap.add_argument("--base", default=None, help="服务地址（默认 http://127.0.0.1:8021）")
    ap.add_argument("--with-closeout", action="store_true", help="多跑收口演示验收（会留两行台账）")
    args = ap.parse_args()

    if args.base:
        BASE = args.base.rstrip("/")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    fails: list[str] = []
    warns: list[str] = []
    t_all = time.time()
    log("=== 答辩前自检（只读；不改数据）===\n")

    # 1 依赖
    log("[1/6] 环境与依赖")
    if not VENV_PY.exists():
        fails.append("venv 缺失：%s" % VENV_PY)
        log("  [X] venv 不在，后面全部无意义")
    else:
        log(f"  [ok] venv：{VENV_PY}")
        log(f"  [ok] 服务目录：{HERE}")

    # 2 服务
    log("\n[2/6] 服务 8021")
    try:
        h = http_json("/healthz")
        log(f"  [ok] healthz ok · 连库 {h.get('db_ms')} ms")
    except Exception as e:  # noqa: BLE001
        if args.start and start_service():
            h = http_json("/healthz")
            log(f"  [ok] 已拉起 · 连库 {h.get('db_ms')} ms")
        else:
            fails.append("8021 没在听")
            log("  [X] 8021 没在听 —— 下一步：")
            log(f'      cd "{HERE}"')
            log('      ..\\fde-diagnosis-agent\\venv\\Scripts\\python.exe -m uvicorn api.app:app '
                '--host 127.0.0.1 --port 8021')
            log("      （或本脚本加 --start 自动拉起）")

    # 3 巡检新鲜度：停用的计划任务不会自己跑，扫描卡会一路变旧
    # 读数走 /v1/obs/runs（checks 里有 silent / hours_since）；/v1/obs/summary 只有接口与新鲜度，没有扫描状态
    log("\n[3/6] 巡检（事件扫描有多旧）")
    try:
        runs = http_json("/v1/obs/runs?limit=5")
        sweep = next((c for c in runs.get("checks", []) if c.get("check_id") == "alert_sweep"), None)
        checks = [c for c in runs.get("checks", []) if c.get("check_id") != "alert_sweep"]
        if sweep is None:
            warns.append("没有 alert_sweep 记录")
            log("  [!] 没有扫描记录 —— 先跑一次 scripts\\patrol.py")
        else:
            hrs = float(sweep.get("hours_since") or 0)
            minute = int(hrs * 60)
            if sweep.get("silent"):
                warns.append("事件扫描已静默 %d 分钟" % minute)
                log(f"  [!] 扫描已 {minute} 分钟没跑（阈值 {runs.get('threshold_hours')}h）"
                    " —— 上台前跑一次 scripts\\patrol.py 或 Start-ScheduledTask \"FDE-Patrol\"")
            else:
                    log(f"  [ok] 扫描 {minute} 分钟前跑过 · 台账判据 {len(checks)} 条")
    except Exception as e:  # noqa: BLE001
        warns.append("读 obs/runs 失败：%s" % e)
        log(f"  [!] 读不到扫描状态：{e}")

    # 4 五个验收
    log("\n[4/6] 五个验收（每个都跑，不因为上一个红了就停）")
    suites = list(SUITES)
    if args.with_closeout:
        suites.append(CLOSEOUT)
    for i, (name, script, timeout) in enumerate(suites, 1):
        ok, tail, dt = run_suite(name, script, timeout)
        log(f"  [{'ok' if ok else 'X'}] {name} · {dt:4.1f}s · {tail}")
        if not ok:
            fails.append(name)

    # 5 页面口径：不看脚本，直接读页面 —— 脚本绿了但页面没更新也会被这一条抓出来
    log("\n[5/6] 页面口径（直接读 /console 的 HTML）")
    try:
        html = http_text("/console")
        need = {
            "首屏三卡": "真错了多少行",
            "待客户答的题": "卡在客户一句话上",
            "工程师折叠区": '<details class="tech">',
            "题面块": "今天要客户点头的几件事",
        }
        miss = [k for k, v in need.items() if v not in html]
        if miss:
            fails.append("页面缺 %s" % miss)
            log(f"  [X] 页面缺：{miss} —— 多半是改了 html 没重启 8021")
        else:
            log("  [ok] 首屏 3 卡 / 题面块 / 折叠区都在")
    except Exception as e:  # noqa: BLE001
        fails.append("读 /console 失败：%s" % e)
        log(f"  [X] 读 /console 失败：{e}")

    # 6 结论
    dt = time.time() - t_all
    log("\n[6/6] 结论")
    for w in warns:
        log(f"  [warn] {w}")
    if fails:
        log(f"  [红] {len(fails)} 项没过：{', '.join(fails)}")
        log("  → 先做第一条：它通常就是根因（服务没起会让后面全红）。")
        return 1
    log(f"  [绿] 全部通过 · 总耗时 {dt:.0f}s · 可以上台"
        + ("（含收口演示，台账已 +2 行）" if args.with_closeout else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
