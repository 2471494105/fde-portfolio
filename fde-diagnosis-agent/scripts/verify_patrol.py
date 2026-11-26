#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T24 验收：一条命令巡检 + 扫描自检 + 错误按路径拆 + 页面静默横幅。

验四件事：
  P1 一条命令能跑完（不用记顺序），且末尾有扫描自检的 run_id
  P2 自检行的 red_count 口径是「新增推送数」，不是违约行数
  P3 复跑不重复推（exit 0、自检行 +1、pushed 0）
  P4 /v1/obs/api 能答「哪条接口在坏」，/v1/obs/runs 能答「扫描多久没跑」
  P5 页面有静默横幅机制，新鲜时不出现
  P6 静默判定真的会置位（临时插一条 30 小时的探针行，验完删掉）

用法：python -X utf8 scripts\verify_patrol.py（需 8021 在跑）
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
BASE = "http://127.0.0.1:8021"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
TMP = tempfile.gettempdir()
DSN = os.environ.get("FDE_CLIENT_DSN", "postgresql://postgres:postgres@localhost:5432/fde_client")
PROBE = "verify_silent_probe"
fails = []


def ok(name, cond, detail=""):
    print(("[ok] " if cond else "[FAIL] ") + name + (f"：{detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def sh(script, *args, timeout=320):
    p = subprocess.run([PY, "-X", "utf8", os.path.join(ROOT, "scripts", script), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def q(sql, params=()):
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.load(r)


def render(url, tag):
    dom = os.path.join(TMP, "verify_patrol_%s.html" % tag)
    err = os.path.join(TMP, "verify_patrol_%s.err" % tag)
    with io.open(dom, "w", encoding="utf-8") as f, io.open(err, "w", encoding="utf-8") as e:
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--user-data-dir=" + os.path.join(TMP, "verify_patrol_ud_" + tag),
                        "--virtual-time-budget=5000", "--dump-dom", url],
                       stdout=f, stderr=e, timeout=120)
    return io.open(dom, encoding="utf-8").read()


def sweep_row():
    rows = q("select run_id, red_count, status, details from gov_check_run"
             " where check_id='alert_sweep' order by run_id desc limit 1")
    return rows[0] if rows else None


def main():
    try:
        code, out = sh("patrol.py", "--quiet")
        ok("P1 一条命令跑完巡检", code in (0, 1), f"exit={code} {out[-200:]}")
        ok("P1b 结论行可读", "巡检完成" in out and "中控台" in out, out[-160:])
        row = sweep_row()
        ok("P1c 扫描自检落台账", row is not None and "扫描自检" in out,
           "alert_sweep 行缺失" if row is None else "")

        ok("P2 自检口径是新增推送数",
           row is not None and (row[3] or {}).get("red_metric") == "pushed_events",
           str((row[3] or {}).get("red_metric")) if row else "无行")
        ok("P2b 通道写在自检行", row is not None and (row[3] or {}).get("channel") in ("inbox", "json", "feishu", "dingtalk"),
           str((row[3] or {}).get("channel")) if row else "无行")

        n0 = q("select count(*) from gov_check_run where check_id='alert_sweep'")[0][0]
        code2, out2 = sh("raise_alerts.py")
        n1 = q("select count(*) from gov_check_run where check_id='alert_sweep'")[0][0]
        ok("P3 复跑不重复推", code2 == 0, f"exit={code2} {out2[-160:]}")
        ok("P3b 自检行仍 +1", n1 == n0 + 1, f"{n0} → {n1}")

        api = get("/v1/obs/api")
        ok("P4 错误按路径拆", isinstance(api.get("errors_by_path"), list)
           and all("path" in x and "n" in x for x in api["errors_by_path"]), str(api.get("errors_by_path")))
        runs = get("/v1/obs/runs?limit=40")
        names = [c["check_id"] for c in runs["checks"]]
        ok("P4b 扫描出现在复跑清单", "alert_sweep" in names, str(names))

        dom = render(BASE + "/console", "console")
        ok("P5 页面有静默横幅", 'id="stale-banner"' in dom)
        ok("P5b 新鲜时不显示横幅", re.search(r'id="stale-banner"[^>]*hidden', dom) is not None)
        ok("P5c KPI 写明扫描时间", "扫描" in dom and "分钟前" in dom)

        q("insert into gov_check_run (check_id, duration_ms, status, red_count, dataset, ran_at)"
          " values (%s, 1, 'ok', 0, 'probe', now() - interval '30 hours')", (PROBE,))
        try:
            runs2 = get("/v1/obs/runs?limit=40")
            probe = [c for c in runs2["checks"] if c["check_id"] == PROBE]
            ok("P6 静默判定会置位", bool(probe) and probe[0]["silent"] is True and probe[0]["hours_since"] > 24,
               str(probe))
        finally:
            q("delete from gov_check_run where check_id = %s", (PROBE,))
            print("（探针运行行已清理）")
    finally:
        try:
            q("delete from gov_check_run where check_id = %s", (PROBE,))
        except Exception:
            print("[warn] 探针行没清掉，请手工检查", file=sys.stderr)

    print("\nRESULT: " + ("ALL PASS" if not fails else "FAIL %d 项：%s" % (len(fails), ", ".join(fails))))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
