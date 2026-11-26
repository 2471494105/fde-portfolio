#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中控台事件链路验收（A1–A6）：从台账到事件表，再到认领/关闭与投递失败。

为什么这一段必须单独验：单测只证明「判定函数」对，证明不了「库里的状态机」对。
一条告警最贵的错误不是算错行数，而是：修好了还挂着、或者点一下就当修好了。
所以这里只看状态机的三件事——重复不增条、认领要有人、关闭要有原因。

前置：客户库已建 gov_alert（sql/014_alerts.sql）。
用法：python -X utf8 scripts/verify_alerts.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
DSN = os.environ.get("FDE_CLIENT_DSN", "postgresql://postgres:postgres@localhost:5432/fde_client")
PROBE = "verify_probe:red"

fails: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    print(("[ok] " if cond else "[FAIL] ") + name + (f"：{detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run([PY, "-X", "utf8", os.path.join(ROOT, "scripts", "raise_alerts.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def q(sql: str, params=()):
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def main() -> int:
    try:
        # A1 表与唯一索引：同一个问题同时只许一条活着
        cols = [r[0] for r in q("select column_name from information_schema.columns"
                               " where table_schema='public' and table_name='gov_alert'")]
        ok("A1 事件表已建", "dedup_key" in cols and "closed_reason" in cols)
        idx = q("select indexdef from pg_indexes where schemaname='public' and tablename='gov_alert'")
        ok("A1b 唯一索引只约束活着的", any("state <> 'closed'" in r[0] for r in idx))

        # A2 dry-run 不写库：先看它要做什么，再决定让它做
        before = q("select count(*) from gov_alert")[0][0]
        code, out = run("--dry-run")
        ok("A2 dry-run 成功", code == 0, out[-200:])
        ok("A2b dry-run 不写库", q("select count(*) from gov_alert")[0][0] == before)

        # A3 真实扫描：事件落库，同 key 只有一条
        code, out = run()
        ok("A3 首次扫描（有新增→exit 1）", code in (0, 1), out[-200:])
        keys = [r[0] for r in q("select dedup_key from gov_alert where state <> 'closed'")]
        ok("A3b 活事件不重复", len(keys) == len(set(keys)), str(keys))

        n1 = q("select seen_count from gov_alert where dedup_key='defect_fields:red'")
        code2, out2 = run()
        n2 = q("select seen_count from gov_alert where dedup_key='defect_fields:red'")
        ok("A4 复跑不重复推（exit 0）", code2 == 0, out2[-160:])
        ok("A4b 轮次仍 +1", n1 and n2 and n2[0][0] == n1[0][0] + 1, f"{n1} → {n2}")

        # A5 状态机：没写 who 的认领不算认领；关闭必须写原因
        q("delete from gov_alert where dedup_key = %s", (PROBE,))
        q("insert into gov_alert (dedup_key, check_id, kind, severity, title, detail, owner)"
          " values (%s, 'verify_probe', 'check_red', 'red', '探针事件', '{}'::jsonb, '验证')", (PROBE,))
        pid = q("select alert_id from gov_alert where dedup_key = %s", (PROBE,))[0][0]
        code, out = run(f"--ack={pid}")
        ok("A5 认领必须写 who", code == 2, out.strip()[-120:])
        code, out = run(f"--ack={pid}", "--by=张工")
        state = q("select state, acked_by from gov_alert where alert_id=%s", (pid,))[0]
        ok("A5b 认领写入状态与责任人", state == ("acked", "张工"), str(state))
        code, out = run(f"--close={pid}")
        ok("A5c 关闭必须写原因", code == 2, out.strip()[-120:])
        code, out = run(f"--close={pid}", "--reason=复跑归零（验证）")
        row = q("select state, closed_reason from gov_alert where alert_id=%s", (pid,))[0]
        ok("A5d 关闭带原因", row[0] == "closed" and "复跑归零" in row[1], str(row))

        # A6 投递失败不许静默：事件还在，detail 里留 delivery 证据
        sys.path.insert(0, ROOT)
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "raise_alerts", os.path.join(ROOT, "scripts", "raise_alerts.py"))
        ra = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ra)
        errs = ra.deliver([{"alert_id": pid, "title": "探针事件", "severity": "red", "owner": "验证"}],
                          "http://127.0.0.1:9/nope", "json", DSN)
        detail = q("select detail from gov_alert where alert_id=%s", (pid,))[0][0]
        ok("A6 投递失败返回错误", bool(errs), str(errs))
        ok("A6b 失败写进事件 detail", isinstance(detail, dict) and detail.get("delivery", {}).get("ok") is False,
           json.dumps(detail, ensure_ascii=False)[:160])

        code, out = run("--list")
        ok("A7 清单可读", code == 0 and "工单/缺陷" in out, out[:120])
    finally:
        try:
            q("delete from gov_alert where dedup_key = %s", (PROBE,))
            print("（探针事件已清理）")
        except Exception:
            print("[warn] 探针事件没清掉，请手工检查", file=sys.stderr)

    print("\nRESULT: " + ("ALL PASS" if not fails else "FAIL %d 项：%s" % (len(fails), ", ".join(fails))))
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        print("验收脚本自身崩了：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
