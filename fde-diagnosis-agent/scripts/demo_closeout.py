#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""收口演示：认领一次、补一行、看红数真的降一格、再复原。

为什么单独做一个脚本
--------------------
判据能红、页面能对上，这两件事已经证明了。没证明的是**「有人动手之后，系统跟得上」**：
  · 认领（ack）之后，事件还在，但不再是「没人管」；
  · 产线补一行 `prod_line`，违约 60/60 → 59/60，合计 362 → 361；
  · 把这一行退回 NULL，又回到 362，且整表逐字与开始时一致。

三段都不许「演示完就留下痕迹」。默认**全程可回放**：修的那一行会复原，认领也会
还原成 open（脚本会明说这是彩排）。正式上场时加 `--keep-ack`，认领就留在页面上。

这一行是**假设**，不是真相
--------------------------
`prod_line` 的值取 `md_station` 字典里的一条（默认「产线A」），证明的是「补上字段
→ 判据认得出」，不证明这张工单真的属于第 1 工位。真实归属要产线写，我们不做反推
（DOC-62）。脚本把这句话打印在修复那一步，防止答辩时说成「我们修好了数据」。

用法
----
    python -X utf8 scripts/demo_closeout.py                  # 全流程，结束复原
    python -X utf8 scripts/demo_closeout.py --dry-run         # 只打印计划，不写库
    python -X utf8 scripts/demo_closeout.py --keep-ack        # 认领保留在页面上
    python -X utf8 scripts/demo_closeout.py --row=WO-2026-0002 --line=产线A
    python -X utf8 scripts/demo_closeout.py --shot=out/ui/console-fix-visible-1280.png

退出码：0 每一段都符合预期（且已复原）/ 1 有断言不符（已尽力复原）/ 2 崩了或复原失败
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import station_contract as sc  # noqa: E402

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
PY = sys.executable


def q(dsn: str, sql: str, params: tuple = ()) -> list[dict]:
    """查询：返回 dict 行。没有 description 的语句（DDL/DML）返回空列表。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return [dict(r) for r in cur.fetchall()]


def fingerprint(dsn: str) -> tuple[str, int]:
    """整表指纹：用来证明「复原」是真的复原，而不是把数字改回去了事。"""
    rows = q(dsn, "SELECT * FROM biz_work_order ORDER BY wo_no")
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], len(rows)


def run_check(dsn: str, record: bool) -> dict:
    """跑一次判据，拿机器可读的结果。回填口径由判据自己决定，这里不改它的数。"""
    tmp = Path(tempfile.gettempdir()) / "demo_closeout_check.json"
    args = [PY, "-X", "utf8", "scripts/check_station_contract.py", f"--dsn={dsn}", f"--json={tmp}"]
    if not record:
        args.append("--no-record")
    p = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    if not tmp.exists():
        raise RuntimeError(f"判据没写出 json（exit={p.returncode}）：{(p.stderr or p.stdout)[-400:]}")
    data = json.loads(tmp.read_text(encoding="utf-8"))
    tmp.unlink(missing_ok=True)
    data["_exit"] = p.returncode
    data["_stdout"] = p.stdout
    return data


def totals(run: dict) -> tuple[int, dict[str, int]]:
    per = {c["id"]: c["violations"] for c in run.get("contracts") or []}
    return sum(per.values()), per


def ids_of(run: dict) -> set[str]:
    """违约行的编号集合。只比条数会漏掉「走了一行、又来了一行」的那种一换一。"""
    return {
        f"{c['id']}#{k}"
        for c in run.get("contracts") or []
        for k in (c.get("keys") or [])
    }


def ids_digest(ids: set[str]) -> str:
    """集合指纹：362 条编号全写进摘要没有意义，留一个可比对的短摘要。"""
    raw = "\n".join(sorted(ids)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def events(dsn: str) -> list[dict]:
    return q(
        dsn,
        "SELECT alert_id, dedup_key, state, seen_count, acked_by, severity"
        " FROM gov_alert ORDER BY alert_id",
    )


def alert_cli(dsn: str, *args: str) -> tuple[int, str]:
    p = subprocess.run(
        [PY, "-X", "utf8", "scripts/raise_alerts.py", f"--dsn={dsn}", *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    return p.returncode, (p.stdout + p.stderr).strip()


def pick_row(dsn: str, registry: dict, want: str | None) -> tuple[dict, dict, str]:
    """挑一行真违约、且补上产线后按字典能唯一解析的工单。挑不出就停，不硬凑。"""
    rows = q(dsn, "SELECT wo_no, station_no, station_id, prod_line FROM biz_work_order ORDER BY wo_no")
    master = q(dsn, "SELECT station_id, station_no, line_no, seq FROM md_station ORDER BY station_id")
    maps = sc.station_maps(master)
    contract = sc.contract_by_id(registry, "wo_station_identity")
    for r in rows:
        if want and r["wo_no"] != want:
            continue
        sid, _why = sc.resolve(r, contract, maps)
        if sid is not None:
            continue  # 已经不违约的行，拿它演示看不出变化
        return r, contract, "候选"
    raise RuntimeError(f"找不到可演示的行（want={want!r}）：要么该行不违约，要么表为空")


def preview(dsn: str, row: dict, contract: dict, line: str) -> str:
    master = q(dsn, "SELECT station_id, station_no, line_no, seq FROM md_station ORDER BY station_id")
    maps = sc.station_maps(master)
    probe = dict(row)
    probe["prod_line"] = line
    sid, why = sc.resolve(probe, contract, maps)
    if sid is None:
        raise RuntimeError(f"{row['wo_no']} 补 prod_line={line!r} 之后仍解析不出工位：{why}")
    return sid


def shot(path: Path) -> tuple[bool, str]:
    """在「已修复」那一瞬间截一张：页面上的 367 要真的变成 366，不能靠嘴说。"""
    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists():
        return False, "本机没有 Edge，跳过截图"
    path.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        [str(edge), "--headless", "--disable-gpu", "--window-size=1280,1400",
         "--virtual-time-budget=6000", f"--screenshot={path}", "http://127.0.0.1:8021/console"],
        capture_output=True, text=True,
    )
    ok = path.exists() and path.stat().st_size > 0
    return ok, f"{path}（{path.stat().st_size // 1024 if ok else 0} KB，exit={p.returncode}）"


def main(argv: list[str]) -> int:
    dsn = os.environ.get("FDE_CLIENT_DSN", CLIENT_DSN)
    dry = False
    keep_ack = False
    record = True
    row_no: str | None = None
    line = "产线A"
    by = "产线计划员"
    shot_at: Path | None = None
    json_out: Path | None = None
    for a in argv:
        if a == "--dry-run":
            dry = True
        elif a == "--keep-ack":
            keep_ack = True
        elif a == "--no-record":
            record = False
        elif a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--row="):
            row_no = a.split("=", 1)[1]
        elif a.startswith("--line="):
            line = a.split("=", 1)[1]
        elif a.startswith("--by="):
            by = a.split("=", 1)[1]
        elif a.startswith("--shot="):
            shot_at = Path(a.split("=", 1)[1]).resolve()
        elif a.startswith("--json="):
            json_out = Path(a.split("=", 1)[1]).resolve()
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    registry = sc.load()
    before_fp, before_n = fingerprint(dsn)
    base = run_check(dsn, record=False)
    base_n, base_per = totals(base)
    base_ids = ids_of(base)
    ev_before = events(dsn)

    print(f"== 收口演示（{datetime.now():%Y-%m-%d %H:%M}）")
    print(f"[1] 基线：违约 {base_n} 行 {base_per} · 未关闭事件 "
          f"{sum(1 for e in ev_before if e['state'] != 'closed')} 条 · 工单表指纹 {before_fp}")
    if base_n == 0:
        print("已经归零，没有可演示的降一格；这一步该换成「关单」。")
    row, contract, _ = pick_row(dsn, registry, row_no)
    target = preview(dsn, row, contract, line)
    alert = next((e for e in ev_before if e["state"] == "open" and e["severity"] == "red"), None)

    print(f"[2] 认领：{'#' + str(alert['alert_id']) + ' open → acked（' + by + '）' if alert else '当前没有 open 的红事件，跳过'}")
    print(f"[3] 预演：{row['wo_no']}（{row['station_no']}，prod_line=NULL）补 prod_line={line} → 解析 {target}（查字典 md_station，不反推设备台账）")
    print(f"[4] 复跑判据：预期 {base_n} → {base_n - 1}（工单 {base_per.get('wo_station_identity')} → "
          f"{base_per.get('wo_station_identity') - 1}）；{'写台账' if record else '不写台账'}")
    print("[5] 复原：prod_line 回 NULL + 复跑 → 预期回到总数；整表指纹必须与 [1] 相同")
    print(f"[6] 收尾：认领{'保留（--keep-ack）' if keep_ack else '还原为 open（彩排）'}")

    if dry:
        print("（--dry-run：以上只是计划，没有写库）")
        return 0

    ok = True
    acked: int | None = None
    fixed = False
    fixed_n: int | None = None
    fixed_ids: set[str] = set()
    missing: list[str] = []
    added: list[str] = []
    fixed_exit: int | None = None
    try:
        if alert:
            rc, out = alert_cli(dsn, f"--ack={alert['alert_id']}", f"--by={by}")
            print(f"    {out.splitlines()[-1] if out else ''}")
            if rc != 0:
                ok = False
                print(f"    [warn] 认领没成功（exit={rc}）")
            else:
                acked = int(alert["alert_id"])
                got = [e for e in events(dsn) if e["alert_id"] == acked][0]
                print(f"    → 现状态 {got['state']}（{got['acked_by'] or '无'}），seen_count 仍 {got['seen_count']}")
                ok = ok and got["state"] == "acked"

        cur = q(dsn, "SELECT prod_line FROM biz_work_order WHERE wo_no = %s", (row["wo_no"],))[0]["prod_line"]
        if cur is not None:
            raise RuntimeError(f"{row['wo_no']} 的 prod_line 本来就有值（{cur!r}），不是「补字段」场景")
        q(dsn, "UPDATE biz_work_order SET prod_line = %s WHERE wo_no = %s", (line, row["wo_no"]))
        fixed = True
        fixed_run = run_check(dsn, record)
        fixed_n, fixed_per = totals(fixed_run)
        fixed_exit = fixed_run["_exit"]
        fixed_ids = ids_of(fixed_run)
        missing, added = sorted(base_ids - fixed_ids), sorted(fixed_ids - base_ids)
        print(f"[4] 实况：违约 {fixed_n} 行 {fixed_per}（判据 exit={fixed_run['_exit']}）")
        print(f"    消失的编号：{missing or '无'} · 新出现的编号：{added or '无'}")
        want = f"wo_station_identity#{row['wo_no']}"
        if missing != [want] or added:
            ok = False
            print(f"    [warn] 预期只消失 {want} 一条且不新增；实际走 {len(missing)} 条、来 {len(added)} 条"
                  "——说明这行不是唯一变量，别拿去讲")
        if shot_at:
            got_shot, note = shot(shot_at)
            print(f"    截图：{note}")
            ok = ok and got_shot
    finally:
        if fixed:
            q(dsn, "UPDATE biz_work_order SET prod_line = NULL WHERE wo_no = %s", (row["wo_no"],))
        if acked and not keep_ack:
            q(dsn, "UPDATE gov_alert SET state = 'open', acked_by = NULL, acked_at = NULL WHERE alert_id = %s",
              (acked,))

    after_fp, after_n = fingerprint(dsn)
    back_run = run_check(dsn, record)
    back_n, back_per = totals(back_run)
    print(f"[5] 实况：违约 {back_n} 行 {back_per} · 工单表指纹 {after_fp}"
          f"（{'与基线一致' if after_fp == before_fp else '与基线不一致，注意'}，{after_n} 行）")
    back_ids = ids_of(back_run)
    print(f"    编号集合：{'与基线逐条一致' if back_ids == base_ids else '与基线不一致'}（{len(back_ids)} 条）")
    ok = ok and back_n == base_n and after_fp == before_fp and back_ids == base_ids

    ev_after = events(dsn)
    ack_note = "认领已保留" if (acked and keep_ack) else "认领已还原"
    same_seen = all(
        (x["seen_count"] == y["seen_count"])
        for x, y in zip(sorted(ev_after, key=lambda e: e["alert_id"]), sorted(ev_before, key=lambda e: e["alert_id"]))
    )
    print(f"[6] 实况：{ack_note} · 事件 {len(ev_after)} 条 · seen_count {'未变' if same_seen else '变了'}"
          f" · 未关闭 {sum(1 for e in ev_after if e['state'] != 'closed')} 条")
    ok = ok and same_seen

    if json_out:
        summary = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "row": row["wo_no"],
            "line": line,
            "alert_id": alert["alert_id"] if alert else None,
            "baseline": {"rows": base_n, "ids_n": len(base_ids), "ids_digest": ids_digest(base_ids),
                         "fp": before_fp,
                         "events": [{k: e[k] for k in ("alert_id", "state", "seen_count", "acked_by")}
                                    for e in ev_before]},
            "fixed": {"rows": fixed_n, "ids_n": len(fixed_ids), "ids_digest": ids_digest(fixed_ids),
                      "missing": missing, "added": added, "exit": fixed_exit},
            "restored": {"rows": back_n, "ids_n": len(back_ids), "ids_digest": ids_digest(back_ids),
                         "fp": after_fp},
            "ok": bool(ok),
        }
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"摘要 → {json_out}")

    print(f"结论：{'每一段都对得上，且已复原' if ok else '有断言不符（上面 [warn] 就是）'}")
    if not ok:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("演示脚本崩了（数据可能停在半路，先看 prod_line 是否已还原）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
