r"""定时巡检入口：调一次 `patrol.py`，日志按天落盘（UTF-8），退出码原样带出。

为什么不是 .ps1 / .cmd 包一层
----------------------------
Windows 上试过用 PowerShell 做壳，结果是日志乱码：python 按 UTF-8（`-X utf8`）写，
Windows PowerShell 5.1 的重定向按控制台代码页（GBK）解码，再按 UTF-8 存盘，两头各错一次。
日志看不懂就等于没有日志。所以这层壳改成 python 自己写文件——编码只有一种。

调度怎么挂（两层都要做到，缺一层就是「没人跑」）
--------------------------------------------
    Windows 计划任务：schtasks 或 Register-ScheduledTask，每 30 分钟
        python.exe -X utf8 scripts\patrol_hourly.py
    Linux（生产）：crontab -e
        */30 * * * * cd /srv/fde && venv/bin/python -X utf8 scripts/patrol_hourly.py

三条纪律：
  1. 凭据不进仓库：外发通道从环境变量继承（FDE_ALERT_WEBHOOK / FDE_ALERT_CHANNEL）。
  2. 日志追加不覆盖：logs/patrol_YYYYMMDD.log，能倒查是哪一次、exit 多少。
  3. 并发让路交给 patrol.py 的锁（exit 75 = 上一次还在跑），本层不重试、不排队。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGDIR = ROOT / "logs"
PATROL = ROOT / "scripts" / "patrol.py"


def main(argv: list[str]) -> int:
    extra: list[str] = []
    for a in argv:
        if a.startswith("--dsn=") or a.startswith("--webhook=") or a.startswith("--channel="):
            extra.append(a)
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    LOGDIR.mkdir(parents=True, exist_ok=True)
    log = LOGDIR / f"patrol_{datetime.now():%Y%m%d}.log"
    hook = os.environ.get("FDE_ALERT_WEBHOOK", "")
    where = f"外发 {os.environ.get('FDE_ALERT_CHANNEL') or '未设通道'}" if hook else "无凭据，落投递箱"
    head = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始（pid {os.getpid()}，{where}）"

    cp = subprocess.run([sys.executable, "-X", "utf8", str(PATROL), "--quiet", *extra],
                        capture_output=True, text=True, encoding="utf-8", timeout=1800)
    tail = (f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 结束 exit={cp.returncode}")

    with log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(head + "\n")
        if cp.stdout:
            f.write(cp.stdout if cp.stdout.endswith("\n") else cp.stdout + "\n")
        if cp.stderr:
            f.write("[stderr] " + cp.stderr.replace("\n", "\n[stderr] ").rstrip() + "\n")
        f.write(tail + "\n")

    print(cp.stdout.strip() if cp.stdout else "")
    print(tail)
    return cp.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
