#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「已有的判据」接进中控台：跑一遍契约与值域两道，把结果落成台账行。

为什么需要这一层
--------------
判据本身早就有，但它们只把结果打在终端上：中控台读的是 `gov_check_run`，
没落台账的判据在页面上等于不存在。于是客户看到的事件永远只有工单与缺陷两类，
「这台库到底有多少种毛病」这个问题答不出来——不是没判据，是没接线。

这一层只做三件事，不做判定（判定仍在两个独立脚本里，避免第二套口径）：
1. 用子进程跑 `check_data_contract.py --json=…` 与 `check_source_values.py --json=…`；
2. 把它们的结构化结果翻译成台账的四个字段（check_id / status / red_count / ids）；
3. 退出码沿用判据自己的约定（0 绿 / 1 红或黄 / 2 自己出错），不吞、不改写。

口径（写进 DOC-68，页面按这个读）
- `red_count` = **越界或不合规的件数**，不是「行数」也不总是「断言数」：
  契约行 = 失败断言条数 + 隔离区行数；值域行 = 越界列数。
- 严重程度由 `status` 决定（fail → 红，warn → 黄）。值域判据的「黄」是
  「有越界但没到拒跑」，把它写成红，客户会问「你不是说只是提醒吗」。
- 每次跑都会新增一行台账，`ids` 用来和上一次比集合（按编号比，不比条数）。

用法
----
    python -X utf8 scripts/collect_issues.py            # 跑两道并落台账
    python -X utf8 scripts/collect_issues.py --quiet     # 只打结论行
    python -X utf8 scripts/collect_issues.py --no-record # 只看结果，不写台账
    python -X utf8 scripts/collect_issues.py --only=data_contract

退出码：0 都绿 / 1 有红或有黄（预期内）/ 2 判据自己出错（结论不可信）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import runlog  # noqa: E402

PY = sys.executable
DSN_DEFAULT = "postgresql://postgres:postgres@localhost:5432/fde_client"


def _tmp_json() -> str:
    return str(Path(tempfile.mkdtemp(prefix="issues_")) / "out.json")


def _run_judge(script: str, json_path: str) -> tuple[int, str]:
    p = subprocess.run(
        [PY, "-X", "utf8", str(ROOT / "scripts" / script), f"--json={json_path}"],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")


def _read_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def from_contract(payload: dict) -> dict:
    """契约判据 → 台账行。隔离区是「被挡住的行」，失败断言是「登记与库不符」。"""
    fails = [str(x) for x in (payload.get("fail") or [])]
    q = payload.get("quarantine") or {}
    reasons = {str(k): int(v) for k, v in (q.get("reasons") or {}).items()}
    q_rows = int(q.get("rows") or 0)
    ids = [f"assert:{f}" for f in fails] + [f"quarantine:{r}" for r in sorted(reasons)]
    red = len(fails) + q_rows
    return {
        "check_id": "data_contract",
        "status": "fail" if red else "ok",
        "red_count": red,
        "dataset": f"{payload.get('db') or 'fde_client'} @ 契约断言 + stg_quarantine",
        "details": {
            "ids": ids,
            "assert_fails": fails,
            "quarantine_rows": q_rows,
            "quarantine_reasons": reasons,
            "assert_total": payload.get("total"),
            "checked_at": payload.get("checked_at"),
        },
    }


def from_values(payload: dict) -> dict:
    """值域判据 → 台账行。只数「越界」和「必填却空」，`note` 级不计入。"""
    ids: list[str] = []
    counts = {"red": 0, "yellow": 0, "breaking": 0}
    for t in payload.get("tables") or []:
        table = t.get("table") or t.get("name") or "?"
        for f in t.get("findings") or []:
            sev = str(f.get("severity") or "")
            if sev in ("red", "breaking"):
                counts["red" if sev == "red" else "breaking"] += 1
            elif sev == "yellow":
                counts["yellow"] += 1
            else:
                continue
            ids.append(f"{table}:{f.get('column')}")
    red = counts["red"] + counts["breaking"]
    yellow = counts["yellow"]
    status = "fail" if red else ("warn" if yellow else "ok")
    return {
        "check_id": "source_values",
        "status": status,
        "red_count": red + yellow,
        "dataset": f"{payload.get('client') or 'client_a'} @ 登记册值域",
        "details": {
            "ids": sorted(set(ids)),
            "red": counts["red"],
            "breaking": counts["breaking"],
            "yellow": yellow,
            "tables": len(payload.get("tables") or []),
        },
    }


JOBS = [
    ("data_contract", "check_data_contract.py", from_contract),
    ("source_values", "check_source_values.py", from_values),
]


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    record = "--no-record" not in argv
    only = ""
    dsn = os.environ.get("FDE_CLIENT_DSN", DSN_DEFAULT)
    for a in argv:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
        elif a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]

    worst = 0
    rows: list[dict] = []
    for check_id, script, translate in JOBS:
        if only and only != check_id:
            continue
        t0 = time.perf_counter()
        jp = _tmp_json()
        code, out = _run_judge(script, jp)
        if code >= 2:
            print(f"[崩] {check_id}（{script}）exit={code}：", file=sys.stderr)
            print(out[-800:], file=sys.stderr)
            worst = 2
            continue
        row = translate(_read_json(jp))
        row["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        rows.append(row)
        if not quiet:
            for ln in out.splitlines():
                print("    " + ln)
        if row["status"] == "fail":
            worst = max(worst, 1)
        elif row["status"] == "warn" and worst == 0:
            worst = 1

    for r in rows:
        print(f"[{r['status']:4s}] {r['check_id']:<14} {r['red_count']} 件"
              f"（ids {len(r['details'].get('ids') or [])} 个 · {r['duration_ms']} ms）")
        if record:
            try:
                run_id = runlog.record(
                    r["check_id"], r["status"], r["red_count"], r["duration_ms"],
                    dataset=r["dataset"], details=r["details"], dsn=dsn,
                )
                print(f"        台账 → run_id={run_id}")
            except Exception as e:  # 写不进去要说出来：缺一条台账，页面就会掉一类
                print(f"[warn] 台账没写进去：{type(e).__name__}: {e}", file=sys.stderr)
                worst = 2
    if not rows:
        print("[warn] 没有跑任何判据（--only 写错？）", file=sys.stderr)
        return 2
    return worst


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("接线脚本自己崩了（这一轮不能当「没问题」）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
