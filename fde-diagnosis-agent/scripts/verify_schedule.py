r"""定时巡检的验收：**证明它真的会自己跑**，而不是「我注册过」。

五条：
  S1 计划任务存在，动作指向本仓 venv 的 python + `scripts/patrol_hourly.py`，工作目录是本仓
  S2 重复间隔是 30 分钟（PT30M）
  S3 今天有日志，且里面是「判据 8 道」那一版（不是旧壳写的空日志）
  S4 并发让路还在：造一个锁 → patrol 必须 exit 75 且不写台账
  S5 凭据不进仓库：scripts/ 与 docs/ 里不许出现 http(s) 的 hook 地址
  S5b 地址只落在本机：`.env` 被 .gitignore 覆盖，且不在 git 索引里

Windows 专用（读 `C:\Windows\System32\Tasks\<任务名>` 的 XML）；其它系统上 S1/S2 标 skip，
S3–S5 照跑——「本机没挂计划任务」不等于「这套东西没有定时能力」。

用法：
    python -X utf8 scripts/verify_schedule.py
    python -X utf8 scripts/verify_schedule.py --task=FDE-Patrol --every=30
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TASKS_DIR = Path(r"C:\Windows\System32\Tasks")

ok_n = fail_n = skip_n = 0


def ok(tag: str, msg: str = "") -> None:
    global ok_n
    ok_n += 1
    print(f"[ok] {tag}" + (f"  {msg}" if msg else ""))


def bad(tag: str, msg: str) -> None:
    global fail_n
    fail_n += 1
    print(f"[FAIL] {tag}  {msg}")


def skip(tag: str, msg: str) -> None:
    global skip_n
    skip_n += 1
    print(f"[skip] {tag}  {msg}")


def ns(root: ET.Element) -> str:
    m = re.match(r"\{.*\}", root.tag)
    return m.group(0) if m else ""


def read_task(task: str) -> ET.Element | None:
    p = TASKS_DIR / task
    if not p.exists():
        return None
    return ET.fromstring(p.read_text(encoding="utf-16"))


def main(argv: list[str]) -> int:
    task = "FDE-Patrol"
    every = 30
    for a in argv:
        if a.startswith("--task="):
            task = a.split("=", 1)[1]
        elif a.startswith("--every="):
            every = int(a.split("=", 1)[1])
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    print(f"== 计划任务 {task}（每 {every} 分钟）")
    root = read_task(task) if sys.platform == "win32" else None
    if root is None:
        skip("S1", "没找到计划任务（非 Windows 或未注册）")
    else:
        n = ns(root)
        exe = (root.findtext(f".//{n}Exec/{n}Command") or "").strip().replace('"', "")
        args = (root.findtext(f".//{n}Exec/{n}Arguments") or "").strip()
        wdir = (root.findtext(f".//{n}Exec/{n}WorkingDirectory") or "").strip()
        want_exe = str(ROOT / "venv" / "Scripts" / "python.exe")
        if exe == want_exe:
            ok("S1a 动作用本仓 venv 的 python", exe)
        else:
            bad("S1a 动作用本仓 venv 的 python", f"实际 {exe!r}")
        if "patrol_hourly.py" in args:
            ok("S1b 参数指向 patrol_hourly.py", args)
        else:
            bad("S1b 参数指向 patrol_hourly.py", f"实际 {args!r}")
        if Path(wdir) == ROOT:
            ok("S1c 工作目录是本仓", wdir)
        else:
            bad("S1c 工作目录是本仓", f"实际 {wdir!r}")
        interval = (root.findtext(f".//{n}Repetition/{n}Interval") or "").strip()
        m = re.fullmatch(r"PT(\d+)M", interval)
        if m and int(m.group(1)) == every:
            ok("S2 重复间隔", interval)
        else:
            bad("S2 重复间隔", f"实际 {interval!r}，期望 PT{every}M")

    # S3 今天的日志
    from datetime import datetime

    log = ROOT / "logs" / f"patrol_{datetime.now():%Y%m%d}.log"
    if not log.exists():
        bad("S3 今天有日志", f"没有 {log.name}")
    else:
        text = log.read_text(encoding="utf-8", errors="replace")
        runs = text.count("巡检完成")
        if "判据 8 道" in text:
            ok("S3 今天的日志是 7 道判据那一版", f"{log.name}（{runs} 次巡检，{len(text)} 字节）")
        else:
            bad("S3 今天的日志是 7 道判据那一版", f"{log.name} 里没找到「判据 8 道」")
        stamps = re.findall(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] 结束 exit=(-?\d+)", text, re.M)
        if stamps:
            ok("S3b 日志有结束时间与退出码", f"最近一次 {stamps[-1][0]} exit={stamps[-1][1]}")
        else:
            bad("S3b 日志有结束时间与退出码", "没找到")

    # S4 锁让路：造锁 → 必须 75，且不许写台账
    lock = ROOT / "logs" / "verify.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("pid=verify at=fake", encoding="utf-8")
    try:
        cp = subprocess.run([PY, "-X", "utf8", str(ROOT / "scripts" / "patrol.py"),
                             f"--lock-file={lock}"],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    finally:
        lock.unlink(missing_ok=True)
    if cp.returncode == 75 and "上一次还在跑" in (cp.stdout or ""):
        ok("S4 锁在时第二次巡检让路", "exit=75")
    else:
        bad("S4 锁在时第二次巡检让路", f"exit={cp.returncode}，输出 {((cp.stdout or '')[:80])!r}")

    # S5 凭据不进仓库
    pat = re.compile(r"https://[^\s\"']*(hook|webhook|open\.feishu\.cn/open-apis/bot)", re.I)
    hits = []
    for d in (ROOT / "scripts", ROOT / "docs"):
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".md", ".ps1", ".json", ".txt"):
                try:
                    t = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for line in t.splitlines():
                    if pat.search(line):
                        hits.append(f"{f.relative_to(ROOT)}: {line.strip()[:60]}")
    if not hits:
        ok("S5 仓库里没有 webhook 地址", "地址只在部署侧，仓库里查不到")
    else:
        bad("S5 仓库里没有 webhook 地址", "；".join(hits[:3]))

    # S5b 地址只落在本机：.env 被忽略且未被跟踪
    git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    git_ok = bool(git) and (Path(git).exists() or bool(shutil.which(git)))
    if (ROOT / ".env").exists() and git_ok:
        try:
            ignored = subprocess.run([git, "check-ignore", "-q", ".env"], cwd=str(ROOT),
                                     capture_output=True, timeout=30)
            tracked = subprocess.run([git, "ls-files", "--error-unmatch", ".env"], cwd=str(ROOT),
                                     capture_output=True, timeout=30)
            if ignored.returncode == 0 and tracked.returncode != 0:
                ok("S5b .env 被忽略且未入库", "check-ignore=0, ls-files≠0")
            else:
                bad("S5b .env 被忽略且未入库",
                    f"ignored={ignored.returncode}, tracked={tracked.returncode}")
        except (OSError, subprocess.SubprocessError) as e:
            bad("S5b .env 被忽略且未入库", f"{type(e).__name__}: {e}")
    else:
        skip("S5b .env 被忽略且未入库", "没有 .env 或没有 git")

    print(f"\nRESULT: {ok_n} ok / {fail_n} fail / {skip_n} skip")
    return 1 if fail_n else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
