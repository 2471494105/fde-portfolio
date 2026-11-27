#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一条命令跑完一次巡检：三道判据 → 事件 → 页面数字。

为什么要有这个入口
----------------
到 T23 为止演示要敲四条命令（三条判据 + 一次扫描），顺序错了事件会基于旧台账。
「简易操作」不是把步骤藏起来，而是**把顺序写成代码**：先判据、后事件、再自检，
每一步失败都停在那儿并说清楚，而不是接着往下跑出一堆看起来正常的数字。

用法
----
    python -X utf8 scripts/patrol.py              # 跑巡检（默认不外发，只落投递箱）
    python -X utf8 scripts/patrol.py --quiet      # 只打结论行
    python -X utf8 scripts/patrol.py --webhook=https://... --channel=feishu

退出码：0 巡检完成且无新增事件 / 1 有新增事件需要人处理 / 2 有判据或扫描自身出错
"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

CHECKS = [
    ("station_contract", "工位归属判据 I/J", "check_station_contract.py"),
    ("status_policy", "口径判据 A–H", "check_status_policy.py"),
    ("defect_fields", "缺陷字段判据 K", "check_defect_fields.py"),
    # 契约与值域两道判据早就有，但只打在终端上；T26 由 collect_issues 把结果落成台账，
    # 中控台才会多出「隔离区」「值域」两类事件。判据本身没搬进来，避免第二套实现。
    ("data_contract+source_values", "契约/隔离区 + 源端值域（接线）", "collect_issues.py"),
    # T27：报警闭环。判据进了巡检，才不会「不上中控台就静默」——
    # 一条命令跑不到它，页面上那一行就永远是上一轮的。
    ("alarm_ack", "报警闭环判据 L", "check_alarm_ack.py"),
    # T28：源端结构漂移（S1–S4）。原来它不落台账、也不进巡检，跑到绿也没人知道跑过，
    # 页面上那一行永远写「未跑」——判据不进巡检就等于不存在。
    ("source_schema", "源端结构判据 S1–S4", "check_source_schema.py"),
]


def run_one(rel: str, extra: list[str], quiet: bool) -> tuple[int, str]:
    p = subprocess.run([PY, "-X", "utf8", os.path.join(ROOT, "scripts", rel), *extra],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    out = (p.stdout or "").strip()
    tail = [ln for ln in out.splitlines() if ln.strip()][-1:] if out else []
    if not quiet:
        for ln in out.splitlines():
            print("    " + ln)
    return p.returncode, (p.stderr or "").strip() + ("\n" if p.stderr else "") + (tail[0] if tail else "")


def take_lock(path: str, ttl: int) -> tuple[bool, str]:
    """单实例锁：定时任务和「人手动跑一次」撞车时，后来的那个直接让路。

    为什么需要：T28 之后巡检不再只由人触发（计划任务每 30 分钟一次）。两次
    同时跑会写两行台账、把 seen_count 一次推两轮，页面上的「多久没人动」就失真了。
    锁文件里写 pid 与时间；超过 ttl 秒视为上一位已经崩了，允许抢（不许因为一次
    崩溃就永久不再巡检）。
    """
    import time as _t

    p = Path(path)
    if p.exists():
        try:
            age = _t.time() - p.stat().st_mtime
            if age < ttl:
                body = p.read_text(encoding="utf-8").strip()
                return False, f"上一次还在跑（{age:.0f}s < ttl {ttl}s，锁内容 {body or '空'}）"
        except OSError:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"pid={os.getpid()} at={_t.strftime('%Y-%m-%d %H:%M:%S')}", encoding="utf-8")
    return True, ""


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    extra: list[str] = []
    lock_file = os.path.join(ROOT, "logs", "patrol.lock")
    lock_ttl = 900
    for a in argv:
        if a.startswith("--webhook=") or a.startswith("--channel=") or a.startswith("--dsn="):
            extra.append(a)
        elif a.startswith("--lock-file="):
            lock_file = a.split("=", 1)[1]
        elif a.startswith("--lock-ttl="):
            lock_ttl = int(a.split("=", 1)[1])

    got, why = take_lock(lock_file, lock_ttl)
    if not got:
        print(f"[skip] 本次不跑：{why}")
        print("       定时任务与手动巡检撞车时这样让路，避免一次巡检写两行台账。")
        return 75
    try:
        return _patrol(quiet, extra)
    finally:
        Path(lock_file).unlink(missing_ok=True)


def _patrol(quiet: bool, extra: list[str]) -> int:
    crashed = False
    for check_id, label, script in CHECKS:
        code, tail = run_one(script, [], quiet)
        # 判据自己约定：0 绿 / 1 红（预期内）/ 2+ 自身出错（不许当红也不许当绿）
        if code >= 2:
            crashed = True
            print(f"[崩] {check_id}（{label}）exit={code}：{tail[-400:]}")
        tag = "红" if code == 1 else "绿" if code == 0 else "崩"
        # 接线那一步含两道判据：1 = 有红或有黄，不能只念「红」
        if "+" in check_id and code == 1:
            tag = "红/黄"
        print(f"[{tag}] {check_id}（{label}）")

    print("— 判据跑完，开始扫描事件 —")
    code, tail = run_one("raise_alerts.py", extra, quiet)
    if code >= 2:
        crashed = True
        print(f"[崩] 事件扫描 exit={code}：{tail[-400:]}")
    new_events = 1 if code == 1 else 0
    sweep_line = [ln for ln in tail.splitlines() if "扫描自检" in ln]
    if sweep_line:
        print("    " + sweep_line[-1])

    print("")
    print(f"巡检完成：判据 {len(CHECKS)} 道 · 事件扫描 {'有新增，需人处理' if new_events else '无新增'} · "
          f"{'出现自身错误，结论不可信' if crashed else '中控台 http://127.0.0.1:8021/console 已可看'}")
    if crashed:
        return 2
    return 1 if new_events else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
