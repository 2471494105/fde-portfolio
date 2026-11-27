#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""答辩控制台的后端：在 8021 上开一个新窗口，按钮跑**既有**脚本。

为什么放在 8021 而不另起一个服务
----------------------------
现场只有一个人、一块屏、一个浏览器。多开一个端口就多一个「打不开」的概率。
所以这一页跟中控台同进程、同端口——但**读面仍只走 `ro_reader`**：这个模块不写客户库，
按钮只是 spawn 子进程，真正动库的是 `fde-diagnosis-agent/scripts/*` 里那些脚本，
它们用自己 `.env` 里的账号。这页只负责触发、看输出、看状态。

四条边界（写进 DOC-79，别靠口头）
----------------------------
1. **不读写客户库**：`demo.py` 里没有一条 SQL 写语句；状态灯也只读。
2. **只给本机**：默认只接受 127.0.0.1 / ::1 的调用；客户部署设 `FDE_DEMO_OFF=1`，全部 403。
3. **动作白名单**：不做「输入任意命令」。五个动作写死在 `ACTIONS` 里。
4. **一次只跑一个**：独占槽，第二次点回 409——现场双击不会把同一件事演两遍。

用法（页面）：http://127.0.0.1:8021/demo
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

FDE = Path(__file__).resolve().parents[2] / "fde-diagnosis-agent"
PY = FDE / "venv" / "Scripts" / "python.exe"
OUT = Path(__file__).resolve().parents[1] / "out" / "demo"
TIMEOUT = 240  # 秒；演示里最长的是收口彩排（含截图）

# 动作白名单：name -> (脚本, 参数, 一句话说明, 会不会改数)
ACTIONS: dict[str, tuple[str, list[str], str, str]] = {
    "patrol": (
        "scripts/patrol_hourly.py", [],
        "跑一遍巡检（与计划任务同一个入口）：8 道判据 + 事件扫描，日志落 logs/patrol_*.log",
        "会写台账、可能推群；不改业务数据",
    ),
    "probe": (
        "scripts/probe_red.py", [],
        "演红：把「没样本」的两道判据当场红一次（临时表头 + sandbox 临时表，跑完自删）",
        "不碰客户库；判据没红就 exit 1（那是判据失效，不是演示成功）",
    ),
    "closeout": (
        "scripts/demo_closeout.py", [],
        "收口彩排：认领 → 补一行 362→361 → 复原，六段带校验",
        "会改工单行数并复原；台账多两行",
    ),
    "export": (
        "scripts/export_actions.py", [],
        "把三条活事件合并成一份「待处置清单 CSV」，落 fde 侧 out/actions/",
        "只读 + 写 CSV；不写回任何客户字段",
    ),
}

_LOCK = threading.Lock()
RUN: dict = {"busy": False, "action": None, "started": None, "exit": None,
             "log": None, "note": None, "pid": None}


def disabled() -> bool:
    return os.environ.get("FDE_DEMO_OFF", "") == "1"


def caller_ok(host: str | None, token: str | None) -> tuple[bool, str]:
    if disabled():
        return False, "这页被关掉了（FDE_DEMO_OFF=1）：它是演示脚手架，不进客户环境"
    if host not in ("127.0.0.1", "::1", "localhost"):
        return False, f"只接受本机调用（来源 {host}）：这页能跑脚本，不给局域网开"
    want = os.environ.get("FDE_DEMO_TOKEN", "")
    if want and (token or "") != want:
        return False, "令牌不对（FDE_DEMO_TOKEN）"
    return True, ""


def _watch(proc: subprocess.Popen, fh, action: str) -> None:
    try:
        code = proc.wait(timeout=TIMEOUT)
        note = ""
    except subprocess.TimeoutExpired:
        proc.kill()
        code, note = -9, f"超时 {TIMEOUT}s 被强杀"
    try:
        fh.close()
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        RUN.update({"busy": False, "exit": code, "note": note,
                    "ended": datetime.now().isoformat(timespec="seconds")})


def start(action: str, host: str | None, token: str | None) -> tuple[int, dict]:
    ok, why = caller_ok(host, token)
    if not ok:
        return 403, {"ok": False, "error": why}
    if action not in ACTIONS:
        return 404, {"ok": False, "error": f"不在白名单里：{action}（可选 {', '.join(ACTIONS)}）"}
    with _LOCK:
        if RUN["busy"]:
            return 409, {"ok": False, "error": f"上一件事还在跑：{RUN['action']}（一次只跑一个）"}
        script, args, _, _ = ACTIONS[action]
        if not PY.is_file():
            return 500, {"ok": False, "error": f"找不到 fde 的 venv python：{PY}"}
        OUT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log = OUT / f"{action}_{stamp}.log"
        fh = log.open("w", encoding="utf-8", newline="\n")
        fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] 开始 {action}：{script} {' '.join(args)}\n")
        fh.flush()
        try:
            proc = subprocess.Popen(
                [str(PY), "-X", "utf8", script, *args],
                cwd=str(FDE), stdout=fh, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            fh.close()
            return 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        RUN.update({"busy": True, "action": action, "started": datetime.now().isoformat(timespec="seconds"),
                    "exit": None, "log": log.name, "note": None, "pid": proc.pid, "ended": None})
    threading.Thread(target=_watch, args=(proc, fh, action), daemon=True).start()
    return 202, {"ok": True, "action": action, "log": log.name, "script": script, "args": args}


def log_since(latest: str | None, since: int) -> dict:
    if not latest:
        return {"chunk": "", "next": 0, "done": True, "error": "还没有跑过动作"}
    p = OUT / latest
    if not p.is_file():
        return {"chunk": "", "next": 0, "done": True, "error": f"日志不在了：{latest}"}
    text = p.read_text(encoding="utf-8", errors="replace")
    chunk = text[int(since):]
    return {"chunk": chunk, "next": len(text), "done": not RUN["busy"]}


def snapshot(dsn: str) -> dict:
    """三盏灯：在开事件数、最近一次巡检、当前动作。只读，不重算判据。"""
    import psycopg

    out = {"busy": RUN["busy"], "action": RUN["action"], "exit": RUN["exit"],
           "note": RUN["note"], "log": RUN["log"], "pid": RUN["pid"],
           "started": RUN["started"], "ended": RUN.get("ended"),
           "off": disabled(), "actions": {k: {"desc": v[2], "effect": v[3]} for k, v in ACTIONS.items()}}
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            out["open_events"] = conn.execute(
                "select count(*) from gov_alert where state <> 'closed'").fetchone()[0]
            row = conn.execute(
                "select ran_at, status, red_count from gov_check_run"
                " where check_id = 'alert_sweep' order by run_id desc limit 1").fetchone()
            out["last_patrol"] = (
                {"at": row[0].isoformat(timespec="minutes"), "status": row[1]} if row else None)
    except Exception as e:  # noqa: BLE001
        out["db_error"] = f"{type(e).__name__}: {e}"
    return out


def reset_for_tests() -> None:
    with _LOCK:
        RUN.update({"busy": False, "action": None, "exit": None, "log": None,
                    "note": None, "pid": None, "started": None, "ended": None})
