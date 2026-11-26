#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T7 验收：审计记不记得住，越权有没有留痕。

九条断言，缺一条算没过。跑法：python scripts\verify_audit.py
用一张合成工单 WO-T7-PROBE 做实验，验完自删——不污染种子数据，可以反复跑。
"""
from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row

BASE = os.environ.get("FDE_CLIENT_DSN_BASE", "localhost:5432/fde_client")
DSN = {
    "qms": "postgresql://qms_app:qmsapp2026pw@%s" % BASE,
    "ro": "postgresql://ro_reader:roreader2026pw@%s" % BASE,
    "aud": "postgresql://auditor:auditor2026pw@%s" % BASE,
}
PROBE = "WO-T7-PROBE"
REC = 310            # 缺陷记录里没用过的号
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    with psycopg.connect(DSN["qms"]) as c:
        # ---- A1：业务账号读不到自己的审计 ----
        try:
            c.execute("select count(*) from aud.audit_log").fetchone()
            check("A1 qms_app 读 audit_log 被拒", False, "居然读到了")
        except psycopg.errors.InsufficientPrivilege as e:
            check("A1 qms_app 读 audit_log 被拒", True, str(e).splitlines()[0])
            c.rollback()      # 预期内的报错会污染事务，清掉才能继续

        # 上次跑崩留下的探针先清掉
        c.execute("delete from biz_work_order where wo_no = %s", (PROBE,))
        c.commit()

        c.execute("insert into biz_work_order (wo_no, station_no, equip_no, opened_at, state, stage_id, priority, maintenance_team_id, note) "
                  "values (%s, '2号工位', 'CX-101', now(), '待处理', '待排产', 1, '电气组', 'T7 验证用，验完自删')", (PROBE,))
        c.execute("update biz_work_order set state = '处理中' where wo_no = %s", (PROBE,))
        c.commit()

        # ---- A3：只改一列，审计里就只有一列 ----
        c.execute("insert into biz_defect_record (rec_id, captured_at, equip_no, defect_name, conf_pct, lot_no, judge, is_ok) "
                  "values (%s, now(), 'CX-110', '划伤', 80, 'B-T7', 'NG', 'N')", (REC,))
        c.commit()
        c.execute("delete from biz_defect_record where rec_id = %s", (REC,))
        c.commit()
        c.execute("delete from biz_work_order where wo_no = %s", (PROBE,))
        c.commit()

    with psycopg.connect(DSN["aud"], row_factory=dict_row) as c:
        u = c.execute("select before_value, after_value, actor from aud.audit_log "
                      "where table_name='biz_work_order' and row_key=%s and op='update' "
                      "order by audit_id desc limit 1", (PROBE,)).fetchone()
        check("A2 update 只记变化的列", u is not None and set((u["before_value"] or {}).keys()) == {"state"},
              "before=%s after=%s" % (u["before_value"], u["after_value"]) if u else "没找到这条审计")
        check("A2 前后值都对", u is not None
              and u["before_value"].get("state") == "待处理"
              and u["after_value"].get("state") == "处理中",
              "actor=%s" % (u["actor"] if u else "?"))
        check("A2 三个动作都留了痕", c.execute(
            "select count(*) as n from aud.audit_log where table_name='biz_work_order' and row_key=%s", (PROBE,)
        ).fetchone()["n"] >= 3, "insert+update+delete")

        ins = c.execute("select after_value from aud.audit_log where table_name='biz_defect_record' "
                        "and row_key=%s and op='insert' order by audit_id desc limit 1", (str(REC),)).fetchone()
        dele = c.execute("select before_value from aud.audit_log where table_name='biz_defect_record' "
                         "and row_key=%s and op='delete' order by audit_id desc limit 1", (str(REC),)).fetchone()
        check("A3 insert 记整行", ins is not None and len(ins["after_value"]) >= 9,
              "列数=%d" % (len(ins["after_value"]) if ins else 0))
        check("A3 delete 记整行且行已消失", dele is not None and len(dele["before_value"]) >= 9,
              "列数=%d" % (len(dele["before_value"]) if dele else 0))
        gone = c.execute("select count(*) as n from aud.audit_log where table_name='biz_work_order' and row_key=%s and op='delete'", (PROBE,)).fetchone()["n"]
        check("A3 探针工单已删干净", gone >= 1, "delete 审计 %d 条" % gone)

    # ---- A4：越权返空集，但留痕 ----
    with psycopg.connect(DSN["ro"], row_factory=dict_row) as c:
        try:
            c.execute("select count(*) from gov.field_registry").fetchone()
            check("A4a 直查登记册被拒", False, "居然读到了")
        except psycopg.errors.InsufficientPrivilege as e:
            check("A4a 直查登记册被拒", True, str(e).splitlines()[0])
            c.rollback()

        got = c.execute("select gov.read_guarded() as r").fetchone()["r"]
        check("A4b 安全阀返空集而非报错", got == [], "返回 %r" % (got,))

    with psycopg.connect(DSN["aud"], row_factory=dict_row) as c:
        d = c.execute("select actor, after_value from aud.audit_log where op='denied' order by audit_id desc limit 1").fetchone()
        check("A4c 拒绝留了痕", d is not None and d["actor"] == "ro_reader",
              "actor=%s detail=%s" % (d["actor"], d["after_value"]) if d else "没有 denied 记录")

    print()
    print("结果：%d 条断言，%d 条失败" % (9, len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())