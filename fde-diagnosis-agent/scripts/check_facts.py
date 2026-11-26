"""事实层（L1）的双端验收：改一个值必须留下版本，同键两条当前值必须被拒。

七个必须回答的问题：
1. 一条事实从哪来            -> stg_devices 的当前值摊平，每个属性一条
2. 值变了会发生什么          -> 旧行写 valid_to、新行 supersedes 指旧行、视图只剩 1 行
3. 值没变会发生什么          -> 不写新行（判 hash），重复同步 0 关 0 开
4. 同键能不能有两条当前值    -> 部分唯一索引拒绝；拒不了就是装饰
5. 历史会不会被检查脚本清掉  -> 不会：facts 在主库，检查 TRUNCATE 的是沙箱
6. 和当前值对不对得上        -> facts_current 与 stg_devices 逐键比对，差一个就红
7. 上游少给一个属性会怎样    -> 不关（保持当前值望），但要露在 uncovered 里

跨两个库是刻意的：事实在主库 fde_rag，当前值在沙箱 fde_sandbox。
这不是「没设计好」，是第 5 条要回答的东西——检查脚本每轮清沙箱，历史必须活下来。

用法：
    python scripts/check_facts.py --json=<path>
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ds.facts import (  # noqa: E402
    FACT_KEYS,
    fact_value,
    plan_facts,
    plan_issues,
    rows_to_facts,
)

PROD_DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"
SBX_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"
SQL = ROOT / "data" / "facts.sql"

TENANT = "client_a"
SOURCE = "facts_check"

# 夹具照抄 data/external/devices.csv：不拿库里剩下的行凑数，
# 否则「一致」这条判据会变成「和它自己比」。
BASE = [
    {"device_id": "CX-101", "line": "3号线", "station": "1-3号工位",
     "camera_model": "In-Sight 2801", "lens_model": "16mm", "light_model": "CA-DC21E",
     "install_date": "2025-03-12", "maintenance_cycle_days": 90},
    {"device_id": "CX-102", "line": "3号线", "station": "1-3号工位",
     "camera_model": "In-Sight 2802", "lens_model": "16mm", "light_model": "CA-DC21E",
     "install_date": "2025-05-20", "maintenance_cycle_days": 90},
    {"device_id": "CX-103", "line": "3号线", "station": "1-3号工位",
     "camera_model": "CV-X420 + CA-H200CX", "lens_model": "16mm", "light_model": "CA-DC21E",
     "install_date": "2024-11-08", "maintenance_cycle_days": 90},
    {"device_id": "CX-104", "line": "4号线", "station": "1-2号工位",
     "camera_model": "CV-X450 + CA-H200CX", "lens_model": "16mm", "light_model": "CA-DC21E",
     "install_date": "2024-08-15", "maintenance_cycle_days": 90},
    {"device_id": "CX-105", "line": "4号线", "station": "1-2号工位",
     "camera_model": "In-Sight 2800", "lens_model": "16mm", "light_model": "CA-DC21E",
     "install_date": "2025-09-01", "maintenance_cycle_days": 90},
]


def ensure_schema(prod):
    """逐条执行，不整串发送。

    PG 会把一次多语句查询包进一个隐式事务：其中任意一条报错，整串连同前面已成功的
    CREATE TABLE 一起回滚。第一次跑就踩到了——索引名和视图名撞车，结果连 schema 都
    没建成。逐条发让每条各自成立，报错也指得准。
    """
    for stmt in SQL.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            prod.execute(stmt)
    prod.commit()


def reset_facts(prod):
    # 自引用外键（supersedes -> id）让裸 TRUNCATE 被 PG 拒绝，要带 CASCADE。
    # CASCADE 在这里只波及它自己，因为引用它的表只有它自己。
    prod.execute("TRUNCATE facts.device_fact RESTART IDENTITY CASCADE")
    prod.commit()


def load_devices(sbx, rows):
    """把夹具写进沙箱的当前值表。TRUNCATE 是刻意的：这一块要独立于别的检查块。

    按「这一行真的给了哪些列」插入，不补齐成 NULL：`stg_devices` 本来就没有约束，
    上游少给一列在库里就是 NULL，而 NULL 正是「没给」和「给了空值」混在一起的地方。
    """
    sbx.execute("TRUNCATE stg_devices")
    for r in rows:
        cols = [c for c in ("device_id",) + FACT_KEYS if c in r]
        vals = [r[c] for c in cols]
        sql = (f"INSERT INTO stg_devices ({', '.join(cols)}, _src) "
               f"VALUES ({', '.join(['%s'] * len(cols))}, %s)")
        sbx.execute(sql, vals + [SOURCE])
    sbx.commit()


def read_current(prod):
    rows = prod.execute(
        "SELECT id, entity_id, fact_key, value_hash FROM facts.device_fact_current"
    ).fetchall()
    return {(r["entity_id"], r["fact_key"]): {"id": r["id"], "value_hash": r["value_hash"]}
            for r in rows}


def count_rows(prod):
    return prod.execute("SELECT count(*) AS n FROM facts.device_fact").fetchone()["n"]


def current_rows(prod):
    return prod.execute("SELECT count(*) AS n FROM facts.device_fact_current").fetchone()["n"]


def sync(prod, sbx, source=SOURCE):
    """一轮同步：读沙箱当前值，写主库事实。

    事务 + advisory lock 是必要的：两个同步实例并发时，单靠「先读后写」会同时
    通过 plan（都看到没有当前值），第二条 INSERT 撞上部分唯一索引直接报错。
    锁把并发串行化，唯一索引是第二道防线（防止有人在别处绕过这个函数）。
    """
    rows = sbx.execute("SELECT * FROM stg_devices").fetchall()
    incoming = rows_to_facts(rows)

    with prod.transaction():
        prod.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
                     (f"facts:{TENANT}",))
        current = read_current(prod)
        plan = plan_facts(current, incoming, tenant=TENANT, source=source)

        for c in plan["closes"]:
            # valid_to IS NULL 条件不能省：重复关同一条会让时间窗变成负的
            prod.execute(
                "UPDATE facts.device_fact SET valid_to = now() "
                "WHERE id = %s AND valid_to IS NULL", (c["id"],))
        for o in plan["opens"]:
            prod.execute(
                """INSERT INTO facts.device_fact
                   (tenant, entity_id, fact_key, value, value_hash, valid_from,
                    valid_from_basis, ingested_at, supersedes, source)
                   VALUES (%s, %s, %s, %s::jsonb, %s, now(), %s, now(), %s, %s)""",
                (o["tenant"], o["entity_id"], o["fact_key"],
                 json.dumps(o["value"], ensure_ascii=False), o["value_hash"],
                 o["valid_from_basis"], o["supersedes"], o["source"]),
            )
    return plan


def uniqueness_probe(prod):
    """手插一条同键的第二条当前值：数据库必须拒绝。

    这条测的不是我们的代码，是那道约束还在不在。「拒不了就是装饰」——
    没有这一步，后面所有验收都可能只是代码自己说自己对。
    """
    base = prod.execute(
        "SELECT tenant, entity_id, fact_key, valid_from_basis FROM facts.device_fact_current "
        "ORDER BY id LIMIT 1").fetchone()
    params = (base["tenant"], base["entity_id"], base["fact_key"],
              json.dumps("重复值", ensure_ascii=False), "dup_probe",
              base["valid_from_basis"], "dup_probe")
    sql = ("INSERT INTO facts.device_fact (tenant, entity_id, fact_key, value, value_hash, "
           "valid_from, valid_from_basis, source) "
           "VALUES (%s, %s, %s, %s::jsonb, %s, now(), %s, %s)")
    try:
        prod.execute(sql, params)
    except UniqueViolation as e:
        return {"rejected": True, "constraint": getattr(e.diag, "constraint_name", None)}
    # 没被拒：清掉探针行，但结论记成红
    prod.execute("DELETE FROM facts.device_fact WHERE source = 'dup_probe'")
    return {"rejected": False, "constraint": None}


def consistency(prod, sbx):
    """facts_current 与 stg_devices 在「当前值」上必须逐键一致。

    两套真相并存在**历史维度**（源端口径的现在 vs 我方口径的当时），
    但在**当前维度**必须一样——不一样就是 bug，不是设计。
    这一条是从根上防「两套真相打架」：只靠人去记得核对，迟早会漏。
    """
    live = sbx.execute("SELECT * FROM stg_devices ORDER BY device_id").fetchall()
    facts = {(r["entity_id"], r["fact_key"]): r["value"] for r in prod.execute(
        "SELECT entity_id, fact_key, value FROM facts.device_fact_current").fetchall()}
    checked, bad = 0, []
    live_keys = set()
    for r in live:
        for k in FACT_KEYS:
            checked += 1
            key = (r["device_id"], k)
            live_keys.add(key)
            want = fact_value(r.get(k))
            if facts.get(key) != want:
                bad.append({"device_id": r["device_id"], "fact_key": k,
                            "stg": want, "facts": facts.get(key)})
    # 反向：facts 里有、当前值表里没有的键（设备已经不在上游了）。
    # 不判红——那可能是刻意的保留；但要报数，不能装作没看见。
    extra = [f"{k[0]}.{k[1]}" for k in facts if k not in live_keys]
    return {"checked": checked, "mismatched": bad, "extra_in_facts": extra}


def survives_reset(prod, sbx, restore):
    """沙箱被 TRUNCATE 之后，历史还在不在。

    这是「facts 不放沙箱」唯一能证明的方式：真的清一次，再查一次。
    讲道理不算证据——检查脚本本来就每轮 TRUNCATE。
    """
    before = count_rows(prod)
    sbx.execute("TRUNCATE stg_devices")
    sbx.commit()
    audit = sbx.execute("SELECT count(*) AS n FROM stg_devices").fetchone()["n"]
    after = count_rows(prod)
    load_devices(sbx, restore)       # 恢复成与 facts 一致的状态，别的检查块之后还要用它
    return {"facts_before": before, "facts_after": after,
            "sandbox_rows_after_truncate": audit, "restored_rows": len(restore)}


def version_chain(prod, entity_id, fact_key):
    """某个键的版本链：从最老到最新，每行给出有效区间与被谁取代。"""
    rows = prod.execute(
        "SELECT id, value, valid_from, valid_to, supersedes FROM facts.device_fact "
        "WHERE entity_id = %s AND fact_key = %s ORDER BY id", (entity_id, fact_key)).fetchall()
    return [
        {"id": r["id"], "value": r["value"],
         "valid_from": r["valid_from"].strftime("%Y-%m-%d %H:%M:%S"),
         "valid_to": r["valid_to"].strftime("%Y-%m-%d %H:%M:%S") if r["valid_to"] else None,
         "supersedes": r["supersedes"]}
        for r in rows
    ]


def main(argv=None) -> int:
    json_out = ""
    for arg in (argv if argv is not None else sys.argv[1:]):
        if arg.startswith("--json="):
            json_out = arg.split("=", 1)[1]

    log = []
    with psycopg.connect(PROD_DSN, row_factory=dict_row, autocommit=True) as prod, \
            psycopg.connect(SBX_DSN, row_factory=dict_row, autocommit=True) as sbx:
        ensure_schema(prod)
        reset_facts(prod)
        load_devices(sbx, BASE)

        def step(label, rows=None, source=SOURCE):
            if rows is not None:
                load_devices(sbx, rows)
            plan = sync(prod, sbx, source=source)
            entry = {
                "name": label,
                "closes": len(plan["closes"]),
                "opens": len(plan["opens"]),
                "noops": len(plan["noops"]),
                "uncovered": [f"{u['entity_id']}.{u['fact_key']}" for u in plan["uncovered"]],
                "total_rows": count_rows(prod),
                "current_rows": current_rows(prod),
                "plan_issues": plan_issues(plan),
            }
            log.append(entry)
            return entry

        step("[1] 首轮同步 5 台 × 7 属性（无水可关）")
        step("[2] 同一份值再同步（应 0 关 0 开）")

        # [3] 内容变更：线体改了
        changed = [dict(r) for r in BASE]
        for r in changed:
            if r["device_id"] == "CX-101":
                r["line"] = "1号线B"
        step("[3] CX-101 line -> 1号线B（应 1 关 1 开）", changed)

        # [4] 上游少给属性：在落地表里落地成 NULL，必须被当成「没给」，不许关旧值
        partial = [dict(r) for r in changed if r["device_id"] == "CX-104"]
        two_cols = [{"device_id": r["device_id"], "line": r["line"]} for r in partial]
        step("[4] 只给 CX-104 的两列（其余列在库里是 NULL：不许关旧值）", two_cols)

        # [5] 同键第二条当前值：数据库必须拒绝
        uniq = uniqueness_probe(prod)

        # [6] 当前值一致性（此时 [4] 把 stg_devices 变成了 1 台，先恢复）
        load_devices(sbx, changed)
        cons = consistency(prod, sbx)

        # [7] 沙箱重置后历史存活
        surv = survives_reset(prod, sbx, changed)

        chain = version_chain(prod, "CX-101", "line")
        total = count_rows(prod)
        cur = current_rows(prod)

    print()
    print(f"{'步骤':<52} {'关闭':>4} {'新增':>4} {'不变':>4} {'总行':>5} {'当前':>5}")
    print("-" * 96)
    for e in log:
        print(f"{e['name']:<52} {e['closes']:>4} {e['opens']:>4} {e['noops']:>4} "
              f"{e['total_rows']:>5} {e['current_rows']:>5}")
        if e["uncovered"]:
            shown = ", ".join(e["uncovered"][:5])
            more = f" 等 {len(e['uncovered'])} 个键" if len(e["uncovered"]) > 5 else ""
            print(f"{'':<52} 没给（保持不动）：{shown}{more}")
        if e["plan_issues"]:
            print(f"{'':<52} ⚠ 决策自相矛盾：{'；'.join(e['plan_issues'])}")
    print("-" * 96)
    print(f"CX-101.line 版本链（共 {len(chain)} 行）：")
    for r in chain:
        print(f"  id={r['id']:<4} value={r['value']!r:<10} "
              f"{r['valid_from']} -> {r['valid_to'] or '（当前）':<20} supersedes={r['supersedes']}")
    print()
    print(f"唯一索引     {'拒绝' if uniq['rejected'] else '没拒绝'} "
          f"（{uniq['constraint'] or '—'}）—— 拒不了就是装饰")
    print(f"当前值一致   {cons['checked']} 项比对，不符 {len(cons['mismatched'])} 项；"
          f"facts 多出 {len(cons['extra_in_facts'])} 键")
    print(f"沙箱重置后   facts {surv['facts_before']} → {surv['facts_after']} 行"
          f"（沙箱清空后 {surv['sandbox_rows_after_truncate']} 行，已恢复 {surv['restored_rows']} 台）")
    print()
    print("第 [2] 步是关键：同一份值再同步必须 0 关 0 开，否则历史表会被自己的重复同步撑爆，")
    print("三个月后查起来全是「每天改了一次」的噪声，等于没有历史。")
    print("第 [5] 步是第二道：这条约束拒不了，代码里所有「只保留一条当前值」的说法都不成立。")
    print("第 [7] 步回答「数据存哪里」：facts 在主库、检查清的是沙箱，历史不会被验证动作销毁。")

    issues = []
    for e in log:
        issues.extend(e["plan_issues"])
    if not uniq["rejected"]:
        issues.append("同键第二条当前值没被唯一索引拒绝（约束是装饰）")
    if cons["mismatched"]:
        issues.append(f"facts_current 与 stg_devices 有 {len(cons['mismatched'])} 个键不一致"
                      "（两套真相在当前维度打架）")
    if surv["facts_after"] != surv["facts_before"]:
        issues.append("沙箱 TRUNCATE 把历史一起清掉了（facts 放错了库）")
    if len(chain) != 2:
        issues.append(f"CX-101.line 版本链 {len(chain)} 行（改一次应恰好 2 行）")
    elif chain[0]["valid_to"] is None or chain[1]["supersedes"] != chain[0]["id"]:
        issues.append("版本链不成立：旧行没关或新行没指回旧行")

    status = "ok" if not issues else "fail"

    if json_out:
        payload = {
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "host_db": {"facts": "fde_rag", "current": "fde_sandbox"},
            "policy": {
                "tenant": TENANT,
                "fact_keys": list(FACT_KEYS),
                "valid_from_basis": "ingested",
                "note": ("valid_from 只能取同步时刻（到货），上游不提供生效时刻；"
                         "两次快照之间的中间状态不可见"),
            },
            "steps": log,
            "versioning": {
                "entity_id": "CX-101", "fact_key": "line", "chain": chain,
                "old_closed": chain[0]["valid_to"] is not None if chain else False,
                "new_supersedes_old": (
                    len(chain) == 2 and chain[1]["supersedes"] == chain[0]["id"]),
                "view_single_row": sum(1 for r in chain if r["valid_to"] is None) == 1,
            },
            "uniqueness": uniq,
            "consistency": cons,
            "survives_sandbox_reset": surv,
            "totals": {"rows": total, "current": cur},
            "issues": issues,
            "status": status,
        }
        Path(json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"\nJSON 已写入 {json_out}")

    if issues:
        print("\n问题：")
        for x in issues:
            print(f"  - {x}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
