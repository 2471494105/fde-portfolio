"""事实层（L1）的双端验收：改一个值必须留下版本，同键两条当前值必须被拒。

七个必须回答的问题：
1. 一条事实从哪来            -> stg_devices 的当前值摊平，每个属性一条
2. 值变了会发生什么          -> 旧行写 valid_to、新行 supersedes 指旧行、视图只剩 1 行
3. 值没变会发生什么          -> 不写新行（判 hash），重复同步 0 关 0 开
4. 同键能不能有两条当前值    -> 部分唯一索引拒绝；拒不了就是装饰
5. 历史会不会被检查脚本清掉  -> 不会：验收只写 fcheck 租户，生产 client_a 只计数
6. 和当前值对不对得上        -> facts_current 与 stg_devices 逐键比对，差一个就红
7. 上游少给一个属性会怎样    -> 不关（保持当前值望），但要露在 uncovered 里
8. 生产租户被这一轮碰到没有  -> 每轮前后各数一次行数，相等才叫没动

跨两个库是刻意的：事实在主库 fde_rag，当前值在沙箱 fde_sandbox。
这不是「没设计好」，是第 5 条要回答的东西——检查每轮 TRUNCATE 沙箱，历史必须活下来。

**但「活下来」不能靠一句承诺**：上一版把 `TRUNCATE facts.device_fact` 写在 main 开头，
库里 36 行全是 `source='facts_check'`、`valid_from` 全在同一秒，然后它用
「沙箱重置后 36→36」宣布历史存活——自己就是凶手。现在验收跑在自己的租户里，
生产租户的行数在前后各数一次，第 8 条才是那条证据。

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
    blank_cells,
    fact_hash,
    fact_value,
    plan_facts,
    plan_issues,
    rows_to_facts,
    schema_drift,
)

PROD_DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"
SBX_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"
SQL = ROOT / "data" / "facts.sql"

# 两个租户必须分开，这是这一层最容易犯的错。
#
# 验收要从「一个空的事实层」开始跑七步，因此它必须能清空自己的起点；而生产租户
# 的历史一旦被验收清掉，「版本」这件事就永远不成立——脚本每天跑一次，历史每天
# 只剩一次巡检那么长。第一版直接把 `TRUNCATE facts.device_fact` 写在 main 开头，
# 结果库里 36 行全是 `source='facts_check'`、`valid_from` 全落在同一秒：巡检
# 每天生产一份「历史」，再报「历史存活 ✅」。
#
# 所以：验收只碰 SELFTEST_TENANT，生产租户只读不写，并在每轮前后各数一次行数，
# 用「前后相等」证明验收没有伸手进去（`isolation`）。那一条比任何断言都硬。
PROD_TENANT = "client_a"      # 生产租户：巡检只计数、只读
SELFTEST_TENANT = "fcheck"    # 验收租户：七步在这里跑，允许清掉自己
SOURCE = "facts_check"
# 生产租户那 35 行的真实身份：夹具，不是客户数据。标签分开是为了将来能一条语句
# 清干净（--purge-fixture），而不是让两批数据混在一张表里、谁也说不清哪条是真的。
FIXTURE_SOURCE = "fixture_seed"

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


def reset_tenant(prod, tenant):
    """清掉一个租户的全部行——只清这个租户，不是清表。返回 (删了多少行, 跨租户引用数)。

    四处刻意：

    1. **用 DELETE 不用 TRUNCATE**。`TRUNCATE ... RESTART IDENTITY` 会把全表 id
       序列重置回 1：别的租户还在的行，在巡检后 id 会与新建的行撞号，`supersedes`
       指错行、报告里引用的 id 指向另一条事实。DELETE 保住序列单调。
    2. **先置空本租户的 supersedes 再删**。自引用外键让 DELETE 也会被拒，而
       `TRUNCATE ... CASCADE` 会连别的租户一起波及，这里不能那么干。
    3. **只删 tenant = ?**。第一版是全表 TRUNCATE，等于每天把生产历史清一遍，
       然后用「沙箱重置后 36→36」宣布历史存活——自己就是凶手。
    4. **跨租户引用要先查再清**。如果别的租户的行 `supersedes` 指向本租户的行，
       直接 DELETE 会被外键拒掉：脚本崩了，而真正该说的话（隔离被破坏过）一句没说出来。
       先数一遍当证据，再只清引用、不碰别的租户的行。
    """
    cross = prod.execute(
        "SELECT count(*) AS n FROM facts.device_fact "
        "WHERE tenant <> %s "
        "AND supersedes IN (SELECT id FROM facts.device_fact WHERE tenant = %s)",
        (tenant, tenant)).fetchone()["n"]
    if cross:
        prod.execute(
            "UPDATE facts.device_fact SET supersedes = NULL "
            "WHERE tenant <> %s "
            "AND supersedes IN (SELECT id FROM facts.device_fact WHERE tenant = %s)",
            (tenant, tenant))
    prod.execute(
        "UPDATE facts.device_fact SET supersedes = NULL "
        "WHERE tenant = %s AND supersedes IS NOT NULL", (tenant,))
    n = prod.execute("DELETE FROM facts.device_fact WHERE tenant = %s", (tenant,)).rowcount
    prod.commit()
    return n, cross


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


def read_current(prod, tenant):
    """只读本租户的当前值。

    不带过滤地读全表、却用 `(entity_id, fact_key)` 当键，等于把所有租户的同 ID
    设备挤进同一个槽：两个客户都有 CX-101 时，后读的覆盖先读的，`plan_facts`
    拿错的那条去比 hash，判成「改值」，于是 `closes` 按 id 关掉**另一个客户**的行。
    唯一索引是 `(tenant, entity_id, fact_key)` 三列，跨租户不会冲突、不会报错，
    只会安静地把 A 的历史换成 B 的值。第一版就是这样。
    """
    rows = prod.execute(
        "SELECT id, entity_id, fact_key, value_hash FROM facts.device_fact_current "
        "WHERE tenant = %s", (tenant,)
    ).fetchall()
    return {(r["entity_id"], r["fact_key"]): {"id": r["id"], "value_hash": r["value_hash"]}
            for r in rows}


def count_rows(prod, tenant=None):
    """行数。给 tenant 就只数那个租户——巡检全靠这个数证明自己没动生产。"""
    if tenant is None:
        return prod.execute("SELECT count(*) AS n FROM facts.device_fact").fetchone()["n"]
    return prod.execute(
        "SELECT count(*) AS n FROM facts.device_fact WHERE tenant = %s",
        (tenant,)).fetchone()["n"]


def current_rows(prod, tenant=None):
    if tenant is None:
        return prod.execute(
            "SELECT count(*) AS n FROM facts.device_fact_current").fetchone()["n"]
    return prod.execute(
        "SELECT count(*) AS n FROM facts.device_fact_current WHERE tenant = %s",
        (tenant,)).fetchone()["n"]


def sync(prod, sbx, tenant=SELFTEST_TENANT, source=SOURCE):
    """一轮同步：读沙箱当前值，写主库事实。

    事务 + advisory lock 是必要的：两个同步实例并发时，单靠「先读后写」会同时
    通过 plan（都看到没有当前值），第二条 INSERT 撞上部分唯一索引直接报错。
    锁把并发串行化，唯一索引是第二道防线（防止有人在别处绕过这个函数）。
    """
    rows = sbx.execute("SELECT * FROM stg_devices").fetchall()
    incoming = rows_to_facts(rows)
    # 空白串被当成「没给」跳过的单元格。跳对，但不能静默——调用方要报数。
    blanks = blank_cells(rows)
    live_ids = {str(r["device_id"]) for r in rows if r.get("device_id")}

    # 删除的唯一合法来源是 L-1 的判定（stg_device_state.deleted_at 有值），
    # 而且必须「这一轮快照里确实没有它」。只看「快照里没有」就关，等于把
    # 一次分页超时导致的漏行当成全公司设备报废——那是 L-1 用「缺席两次」才敢下的结论。
    state_rows = sbx.execute(
        "SELECT device_id, deleted_at, deleted_reason FROM stg_device_state "
        "WHERE deleted_at IS NOT NULL").fetchall()
    retire_entities = [
        {"entity_id": s["device_id"], "reason": s["deleted_reason"] or "deleted",
         "deleted_at": s["deleted_at"]}
        for s in state_rows if s["device_id"] not in live_ids
    ]

    with prod.transaction():
        prod.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
                     (f"facts:{tenant}",))
        current = read_current(prod, tenant)
        plan = plan_facts(current, incoming, tenant=tenant, source=source,
                          retire_entities=retire_entities)

        for c in plan["closes"]:
            # valid_to IS NULL 条件不能省：重复关同一条会让时间窗变成负的
            prod.execute(
                "UPDATE facts.device_fact SET valid_to = now(), close_reason = %s "
                "WHERE id = %s AND valid_to IS NULL", ("superseded", c["id"]))
        # 删除关闭：关而不开。valid_to 取 L-1 判删的时刻（那是有来源的时间），
        # 用 GREATEST 兜住「删除时刻早于这条值生效」的异常窗口（否则违反 CHECK）。
        for r in plan["retire"]:
            prod.execute(
                "UPDATE facts.device_fact "
                "SET valid_to = GREATEST(COALESCE(%s::timestamptz, now()), valid_from), "
                "    close_reason = %s "
                "WHERE id = %s AND valid_to IS NULL",
                (r["deleted_at"], f"deleted:{r['reason']}", r["id"]))
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
    # 空白串的痕迹跟着 plan 走：调用方（巡检、报告）能看见「这一轮有几个单元格
    # 被当成没给跳过了」。跳过是对的，沉默不对——上游若真想清空，我们既不清也不报。
    plan["blanks"] = [f"{e}.{k}" for e, k in sorted(blanks)]
    return plan


def uniqueness_probe(prod, tenant=SELFTEST_TENANT):
    """手插一条同键的第二条当前值：数据库必须拒绝。

    这条测的不是我们的代码，是那道约束还在不在。「拒不了就是装饰」——
    没有这一步，后面所有验收都可能只是代码自己说自己对。
    """
    base = prod.execute(
        "SELECT tenant, entity_id, fact_key, valid_from_basis FROM facts.device_fact_current "
        "WHERE tenant = %s ORDER BY id LIMIT 1", (tenant,)).fetchone()
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


def source_coverage(prod, tenant=PROD_TENANT):
    """源端 devices 里每个**有值**的属性，在 facts 当前视图里有没有对应那一行。

    为什么这条判据必须存在：L4 的回指要求结论里的设备事实指得回 facts 里的行。
    若 facts 少一台设备、或少一个属性，那条结论**根本不可能**被回指——
    而 eval 那一层只会看到「这条 claim 找不到对应行」，把它算成模型的错。
    缺口在数据层，就该在数据层红，不能让报告读的人去猜。

    devices 里的空值不算缺口：`rows_to_facts` 会跳过 None 与空白串，
    这类键本来就不该有历史（P1-5 的口径）。只有"有值却没有行"才是缺口。
    """
    rows = prod.execute(
        "SELECT device_id, " + ", ".join(FACT_KEYS) + " FROM devices").fetchall()
    have = {(r["entity_id"], r["fact_key"]) for r in prod.execute(
        "SELECT entity_id, fact_key FROM facts.device_fact_current "
        "WHERE tenant = %s", (tenant,)).fetchall()}
    expected, missing = 0, []
    for d in rows:
        for k in FACT_KEYS:
            v = d[k]
            if v is None or str(v).strip() == "":
                continue
            expected += 1
            if (d["device_id"], k) not in have:
                missing.append(f"{d['device_id']}.{k}")
    return {"devices": len(rows), "expected": expected, "missing": missing}


def consistency(prod, sbx, tenant=SELFTEST_TENANT):
    """facts_current 与 stg_devices 在「当前值」上必须逐键一致。

    两套真相并存在**历史维度**（源端口径的现在 vs 我方口径的当时），
    但在**当前维度**必须一样——不一样就是 bug，不是设计。
    这一条是从根上防「两套真相打架」：只靠人去记得核对，迟早会漏。

    `orphaned_entities` 补的是第三个维度：**实体还在不在**。前两个维度只比
    「两边都有的键」，于是设备被删掉之后，facts 里那条当前值永远不会被拉进比较，
    也就永远不会红——而这恰好是 L1 最该回答的问题：这台设备现在还算不算数。
    第一版把这种情况只塞进 `extra_in_facts` 一个数字里，而调用方不判红。
    """
    live = sbx.execute(
        "SELECT device_id, " + ", ".join(FACT_KEYS) +
        " FROM stg_devices ORDER BY device_id").fetchall()
    facts = {(r["entity_id"], r["fact_key"]): r["value"] for r in prod.execute(
        "SELECT entity_id, fact_key, value FROM facts.device_fact_current "
        "WHERE tenant = %s", (tenant,)).fetchall()}
    live_entities = {r["device_id"] for r in live}
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
    # 反向：facts 里有、当前值表里没有的键。设备已经不在上游了——可能是刻意的保留，
    # 但「当前值」这个视图不能替它继续声称有效。
    extra = [f"{k[0]}.{k[1]}" for k in facts if k not in live_keys]
    orphaned = sorted({str(k[0]) for k in facts if k[0] not in live_entities})
    return {"checked": checked, "mismatched": bad, "extra_in_facts": extra,
            "orphaned_entities": orphaned}


def survives_reset(prod, sbx, restore, tenant=SELFTEST_TENANT):
    """沙箱被 TRUNCATE 之后，历史还在不在。

    这是「facts 不放沙箱」唯一能证明的方式：真的清一次，再查一次。
    讲道理不算证据——检查脚本本来就每轮 TRUNCATE 沙箱。

    注意这条只证明「沙箱那一下没有波及主库」。它**不能**证明「主库没被别的动作
    清掉」，因为主库被清是同一个脚本在更早的位置做的。第一版就在这个位置上
    宣布「历史存活 ✅」，而三行之前它刚把主库 TRUNCATE 过。
    """
    before = count_rows(prod, tenant)
    sbx.execute("TRUNCATE stg_devices")
    sbx.commit()
    audit = sbx.execute("SELECT count(*) AS n FROM stg_devices").fetchone()["n"]
    after = count_rows(prod, tenant)
    load_devices(sbx, restore)       # 恢复成与 facts 一致的状态，别的检查块之后还要用它
    return {"facts_before": before, "facts_after": after,
            "sandbox_rows_after_truncate": audit, "restored_rows": len(restore)}


def version_chain(prod, entity_id, fact_key, tenant=SELFTEST_TENANT):
    """某个键的版本链：从最老到最新，每行给出有效区间与被谁取代。"""
    rows = prod.execute(
        "SELECT id, value, valid_from, valid_to, supersedes FROM facts.device_fact "
        "WHERE tenant = %s AND entity_id = %s AND fact_key = %s ORDER BY id",
        (tenant, entity_id, fact_key)).fetchall()
    return [
        {"id": r["id"], "value": r["value"],
         "valid_from": r["valid_from"].strftime("%Y-%m-%d %H:%M:%S"),
         "valid_to": r["valid_to"].strftime("%Y-%m-%d %H:%M:%S") if r["valid_to"] else None,
         "supersedes": r["supersedes"]}
        for r in rows
    ]


def main(argv=None) -> int:
    json_out = ""
    seed_prod = False
    seed_from = "fixture"
    mark_fixture = False
    purge_fixture = False
    for arg in (argv if argv is not None else sys.argv[1:]):
        if arg.startswith("--json="):
            json_out = arg.split("=", 1)[1]
        elif arg == "--seed-prod":
            seed_prod = True
        elif arg.startswith("--seed-from="):
            seed_from = arg.split("=", 1)[1]
        elif arg == "--mark-fixture":
            mark_fixture = True
        elif arg == "--purge-fixture":
            purge_fixture = True

    log = []
    with psycopg.connect(PROD_DSN, row_factory=dict_row, autocommit=True) as prod, \
            psycopg.connect(SBX_DSN, row_factory=dict_row, autocommit=True) as sbx:
        ensure_schema(prod)

        if seed_prod:
            # 手动入口：把 BASE 当作生产租户的首次导入，之后巡检只读它、绝不再写。
            # 幂等——重复跑是 0 关 0 开。它不在 data_health 的调用路径里，
            # 只能人手执行一次；自动化永远走不到这个分支。
            #
            # source 用 fixture_seed 而不是 initial_load：这 35 行来自夹具，不是客户数据。
            # 打上这个标签，将来真实客户数据接入时才能一条语句把它们清干净
            # （--purge-fixture），而不是混在真实历史里分不出来。
            #
            # --seed-from=devices ：改成从主库 `devices` 取，而不是夹具 BASE。
            # L4 的回指要求 facts 与 devices 逐键一致——拿夹具导出来的行去回指
            # 真实设备档案，指过去的值和 devices 里的不一样，判据会当场挂。
            # 来源是 devices 时 source 记 initial_load（它是真实首导，不是夹具）。
            if seed_from == "devices":
                rows = [dict(r) for r in prod.execute(
                    "SELECT device_id, " + ", ".join(FACT_KEYS) +
                    " FROM devices ORDER BY device_id").fetchall()]
                src = "initial_load"
            else:
                rows = BASE
                src = FIXTURE_SOURCE
            load_devices(sbx, rows)
            plan = sync(prod, sbx, tenant=PROD_TENANT, source=src)
            print(f"生产租户 {PROD_TENANT} 初始化（来源 {seed_from}）：开 {len(plan['opens'])} 条、"
                  f"关 {len(plan['closes'])} 条，现有 {count_rows(prod, PROD_TENANT)} 行"
                  f"（source={src}）")
            return 0

        if mark_fixture:
            # 把早期导入的行补打夹具标签。只改 source 一列，不动值、不动时间。
            # 留着它是因为第一次 --seed-prod 用的是 initial_load，而那个名字
            # 分不出「夹具」和「客户首导」——将来两种数据混在一张表里就说不清了。
            n = prod.execute(
                "UPDATE facts.device_fact SET source = %s "
                "WHERE tenant = %s AND source = %s",
                (FIXTURE_SOURCE, PROD_TENANT, "initial_load")).rowcount
            prod.commit()
            print(f"生产租户 {PROD_TENANT}：{n} 行标为 {FIXTURE_SOURCE}"
                  f"（现在 {count_rows(prod, PROD_TENANT)} 行）")
            return 0

        if purge_fixture:
            # 真实客户数据接入之前，把夹具行清干净。只删 source=fixture_seed 的行，
            # 别的 source 一行不碰；顺序照 reset_tenant：先查跨租户引用，再清引用，再删。
            ids_sql = "SELECT id FROM facts.device_fact WHERE tenant = %s AND source = %s"
            cross = prod.execute(
                "SELECT count(*) AS n FROM facts.device_fact WHERE tenant <> %s "
                "AND supersedes IN (" + ids_sql + ")",
                (PROD_TENANT, PROD_TENANT, FIXTURE_SOURCE)).fetchone()["n"]
            prod.execute(
                "UPDATE facts.device_fact SET supersedes = NULL WHERE tenant <> %s "
                "AND supersedes IN (" + ids_sql + ")",
                (PROD_TENANT, PROD_TENANT, FIXTURE_SOURCE))
            prod.execute(
                "UPDATE facts.device_fact SET supersedes = NULL WHERE tenant = %s "
                "AND supersedes IN (" + ids_sql + ")",
                (PROD_TENANT, PROD_TENANT, FIXTURE_SOURCE))
            n = prod.execute(
                "DELETE FROM facts.device_fact WHERE tenant = %s AND source = %s",
                (PROD_TENANT, FIXTURE_SOURCE)).rowcount
            prod.commit()
            print(f"生产租户 {PROD_TENANT} 夹具已清除：删 {n} 行、清掉跨租户引用 {cross} 条，"
                  f"现在 {count_rows(prod, PROD_TENANT)} 行")
            return 0

        # 生产租户在验收开始前的行数。整段跑完再数一次，两个数相等才叫「没伸手进去」。
        # 这条判据测的不是代码逻辑，是**验收动作本身的副作用**——上一版栽在这儿：
        # 脚本先 TRUNCATE 主库，三条语句之后用「沙箱清空后 36→36」宣布历史存活。
        prod_before = count_rows(prod, PROD_TENANT)
        # 只清验收租户的起点（七步必须从空表开始）。生产租户在这里一个字都不写。
        dropped, cross_refs = reset_tenant(prod, SELFTEST_TENANT)
        load_devices(sbx, BASE)

        def step(label, rows=None, source=SOURCE):
            if rows is not None:
                load_devices(sbx, rows)
            plan = sync(prod, sbx, tenant=SELFTEST_TENANT, source=source)
            entry = {
                "name": label,
                "closes": len(plan["closes"]),
                "opens": len(plan["opens"]),
                "noops": len(plan["noops"]),
                "uncovered": [f"{u['entity_id']}.{u['fact_key']}" for u in plan["uncovered"]],
                "total_rows": count_rows(prod, SELFTEST_TENANT),
                "current_rows": current_rows(prod, SELFTEST_TENANT),
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
        uniq = uniqueness_probe(prod, tenant=SELFTEST_TENANT)

        # [6] 当前值一致性（此时 [4] 把 stg_devices 变成了 1 台，先恢复）
        load_devices(sbx, changed)
        cons = consistency(prod, sbx, tenant=SELFTEST_TENANT)

        # [7] 沙箱重置后历史存活
        surv = survives_reset(prod, sbx, changed, tenant=SELFTEST_TENANT)

        # [9] 设备被上游删掉：L-1 判删（state.deleted_at 有值）+ 快照里没有 →
        # facts 关而不开。这条如果缺，设备删了半年，事实层仍然认为它是当前状态，
        # 而一致性检查只比「两边都还在的键」，永远报绿。
        DELETED = "CX-105"
        sbx.execute(
            "INSERT INTO stg_device_state (device_id, deleted_at, deleted_reason) "
            "VALUES (%s, now(), %s) "
            "ON CONFLICT (device_id) DO UPDATE SET deleted_at = now(), "
            "deleted_reason = EXCLUDED.deleted_reason",
            (DELETED, "event:upstream_purge"))
        sbx.execute("DELETE FROM stg_devices WHERE device_id = %s", (DELETED,))
        sbx.commit()
        plan_del = sync(prod, sbx, tenant=SELFTEST_TENANT, source="delete_check")
        del_rows = prod.execute(
            "SELECT fact_key, valid_to, close_reason FROM facts.device_fact "
            "WHERE tenant = %s AND entity_id = %s ORDER BY fact_key",
            (SELFTEST_TENANT, DELETED)).fetchall()
        still_current = [r["fact_key"] for r in del_rows if r["valid_to"] is None]
        closed_by_delete = [r for r in del_rows
                            if (r["close_reason"] or "").startswith("deleted:")]
        cur_view_rows = prod.execute(
            "SELECT count(*) AS n FROM facts.device_fact_current "
            "WHERE tenant = %s AND entity_id = %s",
            (SELFTEST_TENANT, DELETED)).fetchone()["n"]
        retire_ev = {
            "entity_id": DELETED,
            "retire_count": len(plan_del["retire"]),
            "conflicts": plan_del["retire_conflicts"],
            "rows": len(del_rows),
            "still_current": still_current,
            "closed_by_delete": len(closed_by_delete),
            "closed_by_superseded": len([r for r in del_rows
                                         if r["close_reason"] == "superseded"]),
            "in_current_view": cur_view_rows,
        }

        # 恢复：撤掉删除状态、设备回补、再同步（应 7 开 0 关，值重新成为当前）。
        # 这一下也顺手证明「删除是可撤销的」——与 L-1 的 revive 同一口径。
        # 用 UPDATE 置空而不是 DELETE 行：这一行是 L-1 的状态记录，删了它等于替
        # 删除同步抹掉一段状态，而且别的检查块之后还要用它。
        sbx.execute(
            "UPDATE stg_device_state SET deleted_at = NULL, deleted_reason = NULL "
            "WHERE device_id = %s", (DELETED,))
        sbx.commit()
        load_devices(sbx, changed)
        plan_back = sync(prod, sbx, tenant=SELFTEST_TENANT, source="delete_restore")
        retire_ev["restore_opens"] = len(plan_back["opens"])
        retire_ev["restore_closes"] = len(plan_back["closes"])

        # [10] 多租户：同一个 entity_id 在两个租户下各有一条当前值，改一个不许碰另一个。
        # 这一条测的是 `read_current` 那个 WHERE：不带租户过滤时，两个客户的 CX-101
        # 会挤进同一个槽，plan 判成「改值」，于是按 id 关掉另一个客户的当前行——
        # 唯一索引是 (tenant, entity_id, fact_key) 三列，跨租户不冲突、不报错。
        OTHER = "fcheck_b"
        _, other_cross = reset_tenant(prod, OTHER)
        prod.execute(
            "INSERT INTO facts.device_fact (tenant, entity_id, fact_key, value, value_hash, "
            "valid_from, valid_from_basis, ingested_at, source) "
            "VALUES (%s, 'CX-101', 'line', %s::jsonb, %s, now(), 'ingested', now(), %s)",
            (OTHER, json.dumps("5号线", ensure_ascii=False), fact_hash("5号线"),
             "tenant_probe"))
        other_before = count_rows(prod, OTHER)
        other_hash_before = prod.execute(
            "SELECT value_hash FROM facts.device_fact_current WHERE tenant = %s "
            "AND entity_id = 'CX-101' AND fact_key = 'line'", (OTHER,)).fetchone()
        t2 = [dict(r) for r in changed]
        for r in t2:
            if r["device_id"] == "CX-102":
                r["station"] = "9-9号工位"
        load_devices(sbx, t2)
        sync(prod, sbx, tenant=SELFTEST_TENANT, source="tenant_probe")
        other_after = count_rows(prod, OTHER)
        other_hash_after = prod.execute(
            "SELECT value_hash FROM facts.device_fact_current WHERE tenant = %s "
            "AND entity_id = 'CX-101' AND fact_key = 'line'", (OTHER,)).fetchone()
        tenant_ev = {
            "other_tenant": OTHER,
            "other_rows_before": other_before,
            "other_rows_after": other_after,
            "other_hash_before": other_hash_before["value_hash"] if other_hash_before else None,
            "other_hash_after": other_hash_after["value_hash"] if other_hash_after else None,
            "untouched": (other_before == other_after == 1
                          and other_hash_before is not None
                          and other_hash_after is not None
                          and other_hash_before["value_hash"] == other_hash_after["value_hash"]),
        }

        # [11] 两个「一直没有判据」的缺口：空白串、列漂移。
        #
        # 空白串：上游给 '' 到底算「清空」还是「没给」，协议里没定义。我们的口径是
        # 「没给」——保持当前值不动，但把单元格记进 blanks 让巡检报数。这一条缺了的话，
        # 哪天上游真用空串表达清空，我们会一直认为旧值有效，而且没有任何地方会响。
        t3 = [dict(r) for r in t2]
        for r in t3:
            if r["device_id"] == "CX-103":
                r["lens_model"] = ""
        load_devices(sbx, t3)
        plan_blank = sync(prod, sbx, tenant=SELFTEST_TENANT, source="blank_probe")
        cur_lens = prod.execute(
            "SELECT value FROM facts.device_fact_current WHERE tenant = %s "
            "AND entity_id = 'CX-103' AND fact_key = 'lens_model'",
            (SELFTEST_TENANT,)).fetchone()
        blank_ev = {
            "entity_id": "CX-103",
            "fact_key": "lens_model",
            "blank_seen": "CX-103.lens_model" in plan_blank["blanks"],
            "opens": len(plan_blank["opens"]),
            "closes": len(plan_blank["closes"]),
            "current_value": cur_lens["value"] if cur_lens else None,
            "kept": bool(cur_lens) and cur_lens["value"] == "16mm",
        }
        # 恢复（用 t2，不是 changed——[10] 已经把 CX-102 的工位改成 9-9，
        # 拿 changed 恢复会额外制造一次改值，把行数统计打乱）
        load_devices(sbx, t2)
        sync(prod, sbx, tenant=SELFTEST_TENANT, source="blank_restore")

        # 列漂移：表里每一列业务数据都必须有历史。多一列而 FACT_KEYS 没跟，
        # 那列每轮被覆盖、永久不参与版本化，且没有任何检查会发现——
        # 之前的注释声称「第 [6] 步会拿表结构核对」，而第 [6] 步从来不读列名。
        cols = sbx.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'stg_devices'").fetchall()
        dr = schema_drift([c["column_name"] for c in cols])
        schema_ev = {"columns": [c["column_name"] for c in cols],
                     "fact_keys": list(FACT_KEYS), **dr}

        chain = version_chain(prod, "CX-101", "line", tenant=SELFTEST_TENANT)
        total = count_rows(prod, SELFTEST_TENANT)
        cur = current_rows(prod, SELFTEST_TENANT)

        # [8] 生产租户有没有被这一轮碰到。这是 P0-1 的正面证据：不是「代码里
        # 没写 TRUNCATE」，是真的数了前后两次行数，相等才叫没动。
        prod_after = count_rows(prod, PROD_TENANT)
        prod_span = prod.execute(
            "SELECT min(valid_from) AS f, max(valid_from) AS l "
            "FROM facts.device_fact WHERE tenant = %s", (PROD_TENANT,)).fetchone()
        fmt = "%Y-%m-%d %H:%M:%S"
        isolation = {
            "prod_tenant": PROD_TENANT,
            "prod_rows_before": prod_before,
            "prod_rows_after": prod_after,
            "unchanged": prod_before == prod_after,
            # 生产租户的时间跨度。单看这一轮没用（可能本来就是空的），
            # 但要留给下一轮比：最早一条的 valid_from 变新，就是老历史消失了。
            "prod_first_valid_from": prod_span["f"].strftime(fmt) if prod_span["f"] else None,
            "prod_last_valid_from": prod_span["l"].strftime(fmt) if prod_span["l"] else None,
            "selftest_tenant": SELFTEST_TENANT,
            "selftest_rows_cleared_at_start": dropped,
            # 别的租户的行 supersedes 指向验收租户的行：只有一个解释——某个版本的
            # 读路径漏了租户过滤，跨客户写了引用。正常应为 0。
            "selftest_cross_tenant_refs": cross_refs,
            "selftest_rows": total,
        }

        # 时间跨度（P1-4）：这里只产出证据，不在单轮判红。
        #
        # 「跨度太短」或「只有一批」在任何单轮数据里都可能正常（刚部署、刚切租户）。
        # 判红必须有跨轮基线：曾经有过 2 批以上、之后不再增长，才是同步停摆的迹象。
        # 那条判据在 data_health.facts_drift 里，对着 eval/facts_ledger.json 比。
        # 这里输出 span_hours 与 ingested_batches，让跨轮判断有东西可比。
        cover = prod.execute(
            "SELECT count(*) AS n, min(valid_from) AS f, max(valid_from) AS l, "
            "count(DISTINCT date_trunc('hour', ingested_at)) AS batches "
            "FROM facts.device_fact WHERE tenant = %s", (PROD_TENANT,)).fetchone()
        coverage = {
            "tenant": PROD_TENANT,
            "rows": cover["n"],
            "first_valid_from": cover["f"].strftime(fmt) if cover["f"] else None,
            "last_valid_from": cover["l"].strftime(fmt) if cover["l"] else None,
            "span_hours": (round((cover["l"] - cover["f"]).total_seconds() / 3600, 1)
                           if cover["f"] and cover["l"] else 0.0),
            "ingested_batches": cover["batches"],
        }
        # 算在 with 块里面：devices 和 facts 同在 fde_rag，同一条连接就能查。
        # 放到 print 之后算会踩 "the connection is closed"——块一结束连接就关。
        sc = source_coverage(prod, PROD_TENANT)

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
    print(f"孤儿实体     {len(cons['orphaned_entities'])} 台在 facts 有当前值、但已经不在 stg_devices"
          f"{'（' + '、'.join(cons['orphaned_entities']) + '）' if cons['orphaned_entities'] else ''}")
    de = retire_ev
    print(f"删除传导     {de['entity_id']} 判删 → 关 {de['closed_by_delete']} 条、"
          f"当前值残留 {len(de['still_current'])} 条、视图里 {de['in_current_view']} 条；"
          f"恢复后重开 {de.get('restore_opens')} 条")
    tn = tenant_ev
    print(f"多租户隔离   {tn['other_tenant']} 的 CX-101.line：{tn['other_rows_before']} → "
          f"{tn['other_rows_after']} 行，值指纹 {'未变' if tn['untouched'] else '被改了'}")
    be, se = blank_ev, schema_ev
    print(f"空白串       {be['entity_id']}.{be['fact_key']} 写 '' → "
          f"{'记进 blanks' if be['blank_seen'] else '没记进 blanks'}、"
          f"{be['opens']} 开 {be['closes']} 关，当前值仍 {be['current_value']!r}")
    print(f"列漂移       stg_devices {len(se['columns'])} 列 vs FACT_KEYS "
          f"{len(se['fact_keys'])} 个；表多 {'、'.join(se['missing_in_keys']) or '无'}，"
          f"清单多 {'、'.join(se['extra_in_keys']) or '无'}")
    print(f"生产覆盖     {coverage['tenant']} {coverage['rows']} 行，跨度 "
          f"{coverage['span_hours']} 小时，{coverage['ingested_batches']} 个入库批次"
          f"（单轮只看证据，跨轮判据在 data_health）")
    print(f"源端覆盖     devices {sc['devices']} 台应有 {sc['expected']} 个属性，"
          f"facts 当前缺 {len(sc['missing'])} 个"
          f"{'（' + '、'.join(sc['missing'][:8]) + '）' if sc['missing'] else ''}"
          f"（缺的键不可能被回指，L4 判据会把它算成模型的错）")
    print(f"沙箱重置后   facts {surv['facts_before']} → {surv['facts_after']} 行"
          f"（沙箱清空后 {surv['sandbox_rows_after_truncate']} 行，已恢复 {surv['restored_rows']} 台）")
    print(f"生产租户     {isolation['prod_tenant']} {isolation['prod_rows_before']} → "
          f"{isolation['prod_rows_after']} 行"
          f"（{'未动' if isolation['unchanged'] else '被验收动了'}）；"
          f"验收只清 {isolation['selftest_tenant']}（本轮起点清掉 "
          f"{isolation['selftest_rows_cleared_at_start']} 行）")
    print()
    print("第 [2] 步是关键：同一份值再同步必须 0 关 0 开，否则历史表会被自己的重复同步撑爆，")
    print("三个月后查起来全是「每天改了一次」的噪声，等于没有历史。")
    print("第 [5] 步是第二道：这条约束拒不了，代码里所有「只保留一条当前值」的说法都不成立。")
    print("第 [7] 步回答「数据存哪里」：facts 在主库、检查清的是沙箱。")
    print("第 [8] 步是这一层的护栏：验收只写自己的租户，生产租户行数前后必须相等——")
    print("           上一版正是在这里自证清白的：先 TRUNCATE 主库，再报告「36 → 36 存活」。")

    issues = []
    for e in log:
        issues.extend(e["plan_issues"])
    if not uniq["rejected"]:
        issues.append("同键第二条当前值没被唯一索引拒绝（约束是装饰）")
    if cons["mismatched"]:
        issues.append(f"facts_current 与 stg_devices 有 {len(cons['mismatched'])} 个键不一致"
                      "（两套真相在当前维度打架）")
    if cons["orphaned_entities"]:
        issues.append(f"{len(cons['orphaned_entities'])} 台设备已经不在 stg_devices，"
                      f"但 facts_current 里仍有当前值（删除没传导到 L1）："
                      f"{'、'.join(cons['orphaned_entities'])}")
    if surv["facts_after"] != surv["facts_before"]:
        issues.append("沙箱 TRUNCATE 把历史一起清掉了（facts 放错了库）")
    if not de.get("retire_count"):
        issues.append("设备被判删后没有产生任何关闭（删除没传导到事实层）")
    if de.get("still_current"):
        issues.append(f"{de['entity_id']} 已被判删，但 facts 仍有 "
                      f"{len(de['still_current'])} 条当前值："
                      f"{', '.join(de['still_current'])}")
    if de.get("in_current_view"):
        issues.append(f"{de['entity_id']} 已被判删，但仍出现在 device_fact_current"
                      "（「当前设备清单」里还有它）")
    if de.get("closed_by_delete") != de.get("retire_count"):
        issues.append(
            f"判删关闭数 {de.get('closed_by_delete')} 与计划 {de.get('retire_count')} 不一致"
            "（有关闭没落地）")
    if de.get("conflicts"):
        issues.append(f"标了删除却仍在快照里（自相矛盾，L-1 判据未收敛）："
                      f"{', '.join(de['conflicts'])}")
    if not tenant_ev.get("untouched"):
        issues.append(
            f"多租户隔离失效：改 {SELFTEST_TENANT} 的当前值之后，"
            f"{tenant_ev.get('other_tenant')} 的行数或值指纹变了"
            "（read_current 少了租户过滤，同 ID 设备会跨客户改写）")
    if isolation.get("selftest_cross_tenant_refs"):
        issues.append(
            f"发现 {isolation['selftest_cross_tenant_refs']} 条跨租户 supersedes 引用"
            f"（别的租户的行指向 {SELFTEST_TENANT} 的行）：读路径曾经漏过租户过滤，"
            "版本链已经跨客户了")
    if not isolation["unchanged"]:
        issues.append(
            f"验收动到了生产租户（{isolation['prod_tenant']} "
            f"{isolation['prod_rows_before']} → {isolation['prod_rows_after']} 行）："
            "验收只能写自己的租户")
    if not blank_ev["blank_seen"]:
        issues.append("空白串没被记进 blanks（跳过是静默的：上游若真想清空，没人会知道）")
    if not blank_ev["kept"]:
        issues.append(
            f"空白串改动了 {blank_ev['entity_id']}.{blank_ev['fact_key']} 的当前值"
            "（'' 被当成真值写进历史）")
    if blank_ev["opens"] or blank_ev["closes"]:
        issues.append(f"空白串产生了 {blank_ev['opens']} 开 / {blank_ev['closes']} 关（应为 0/0）")
    if schema_ev["missing_in_keys"]:
        issues.append(
            f"stg_devices 有 {len(schema_ev['missing_in_keys'])} 列业务数据没纳入 FACT_KEYS"
            f"：{'、'.join(schema_ev['missing_in_keys'])}（这些列永久没有历史）")
    if schema_ev["extra_in_keys"]:
        issues.append(
            f"FACT_KEYS 有 {len(schema_ev['extra_in_keys'])} 个键在 stg_devices 里不存在"
            f"：{'、'.join(schema_ev['extra_in_keys'])}（清单已过期）")
    if sc["missing"]:
        issues.append(
            f"devices 里有值的 {len(sc['missing'])} 个属性在 facts 当前视图里没有行："
            f"{'、'.join(sc['missing'][:8])}（这些属性说出来就回指不了）")
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
                "tenant": SELFTEST_TENANT,
                "prod_tenant": PROD_TENANT,
                "fact_keys": list(FACT_KEYS),
                "valid_from_basis": "ingested",
                "note": ("valid_from 只能取同步时刻（到货），上游不提供生效时刻；"
                         "两次快照之间的中间状态不可见。验收只写 "
                         f"{SELFTEST_TENANT}，生产 {PROD_TENANT} 只计数不清"),
            },
            "steps": log,
            "source_coverage": sc,
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
            "isolation": isolation,
            "retirement": de,
            "tenant_isolation": tenant_ev,
            "blank_cells": blank_ev,
            "schema_drift": schema_ev,
            "coverage": coverage,
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
