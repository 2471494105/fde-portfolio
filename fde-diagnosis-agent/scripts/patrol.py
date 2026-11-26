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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

CHECKS = [
    ("station_contract", "工位归属判据 I/J", "check_station_contract.py"),
    ("status_policy", "口径判据 A–H", "check_status_policy.py"),
    ("defect_fields", "缺陷字段判据 K", "check_defect_fields.py"),
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


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    extra: list[str] = []
    for a in argv:
        if a.startswith("--webhook=") or a.startswith("--channel=") or a.startswith("--dsn="):
            extra.append(a)

    crashed = False
    for check_id, label, script in CHECKS:
        code, tail = run_one(script, [], quiet)
        # 判据自己约定：0 绿 / 1 红（预期内）/ 2+ 自身出错（不许当红也不许当绿）
        if code >= 2:
            crashed = True
            print(f"[崩] {check_id}（{label}）exit={code}：{tail[-400:]}")
        print(f"[{'红' if code == 1 else '绿' if code == 0 else '崩'}] {check_id}（{label}）")

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
