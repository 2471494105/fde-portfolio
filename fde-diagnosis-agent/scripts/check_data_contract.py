"""数据断言：查「不该存在的行」。

判据沿用 dbt 的形态：每条断言编成一条 select，只返回坏行；0 行才算通过。

两类断言含义不同，别混着看：

  A 结构类：在 pg 源上由 DB 的 PK / FK / CHECK / 唯一索引保证，跑出来永远 0 行。
            它的价值在换源之后——CSV、外部库、客户自有系统都没有这些约束。
            staging 表刻意不带这些约束，就是为了让 A 类在那里真的会红。

  B 业务类：DB 本来就不管（空串、未来时间、跨表一致性、量级合理性）。

表名用 {占位符} 而不是写死：这样同一套断言能跑在 devices 上，也能跑在
stg_devices 上。跨源测试靠的就是这一点——换的是表，不是判据。

用法：
  python scripts/check_data_contract.py --expect=dirty
  python scripts/check_data_contract.py --prefix=stg_ --expect=dirty
"""

import json
import re
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"

ASSERTIONS = [
    # ---------- B 类：DB 管不了的 ----------
    {
        "id": "B1", "kind": "B", "owner": "设备档案",
        "desc": "设备号不能是空字符串（NOT NULL 只拦 NULL，PK 也允许空串）",
        "sql": "SELECT device_id, line, station FROM {devices} WHERE btrim(device_id) = ''",
    },
    {
        "id": "B2", "kind": "B", "owner": "设备档案",
        "desc": "设备号去空格转小写后必须唯一（防 CX-101 / cx-101 各存一份）",
        "sql": """SELECT lower(btrim(device_id)) AS norm_key,
                         count(*) AS n,
                         string_agg(device_id, ', ' ORDER BY device_id) AS ids
                  FROM {devices} GROUP BY 1 HAVING count(*) > 1""",
    },
    {
        "id": "B3", "kind": "B", "owner": "误判日志",
        "desc": "置信度必须在 0-1",
        "sql": "SELECT id, device_id, confidence FROM {defect_logs} WHERE confidence < 0 OR confidence > 1",
    },
    {
        "id": "B4", "kind": "B", "owner": "误判日志",
        "desc": "日志时间不能在未来（客户系统时钟错、手工补录写错年份）",
        "sql": "SELECT id, device_id, ts FROM {defect_logs} WHERE ts > now()",
    },
    {
        "id": "B5", "kind": "B", "owner": "误判日志",
        # 第一版写成 d.station <> x.station，干净数据上误报 1000 行——两处 station 的
        # 粒度不同：档案记「1-3号工位」（区间），日志记「2号工位」（点）。
        # 这是「断言自身写错」的真实样本，也说明断言必须拿干净数据反向验一次。
        "desc": "日志工位必须落在档案的工位区间内（档案是区间、日志是点）",
        "sql": r"""WITH ranges AS (
                      SELECT device_id,
                             (regexp_match(station, '^(\d+)-(\d+)号工位$'))[1]::int AS lo,
                             (regexp_match(station, '^(\d+)-(\d+)号工位$'))[2]::int AS hi
                      FROM {devices}
                      WHERE station ~ '^\d+-\d+号工位$'
                  )
                  SELECT d.id, d.device_id, d.station AS log_station,
                         r.lo || '-' || r.hi || '号工位' AS archive_range
                  FROM {defect_logs} d
                  JOIN ranges r ON r.device_id = d.device_id
                  WHERE CASE
                          WHEN (regexp_match(d.station, '^(\d+)号工位$'))[1] IS NULL THEN TRUE
                          ELSE (regexp_match(d.station, '^(\d+)号工位$'))[1]::int
                               NOT BETWEEN r.lo AND r.hi
                        END""",
    },
    {
        "id": "B6", "kind": "B", "owner": "误判日志",
        "desc": "批次号至少 4 个字符（防空串、防手滑写成 A）",
        "sql": "SELECT id, device_id, batch FROM {defect_logs} WHERE length(btrim(batch)) < 4",
    },
    {
        "id": "B7", "kind": "B", "owner": "标定记录",
        "desc": "像素漂移与毫米漂移必须同号（一正一负说明换算公式或坐标方向错）",
        "sql": """SELECT id, device_id, drift_px, drift_mm FROM {calibrations}
                  WHERE drift_px <> 0 AND drift_mm <> 0 AND (drift_px * drift_mm) < 0""",
    },
    {
        "id": "B8", "kind": "B", "owner": "光源使用",
        "desc": "累计时长不能超过标称寿命的 3 倍（超了大概率单位错）",
        "sql": """SELECT device_id, cumulative_hours, rated_life_hours FROM {light_usage}
                  WHERE rated_life_hours <= 0 OR cumulative_hours > rated_life_hours * 3""",
    },
    {
        "id": "B9", "kind": "B", "owner": "工单",
        "desc": "工单的设备号必须存在于设备档案（work_orders 没有外键，能塞进去）",
        "sql": """SELECT w.order_id, w.device_id FROM {work_orders} w
                  LEFT JOIN {devices} d ON d.device_id = w.device_id
                  WHERE d.device_id IS NULL""",
    },
    {
        "id": "B10", "kind": "B", "owner": "告警",
        "desc": "告警发生时间不能在未来",
        "sql": "SELECT id, device_id, ts FROM {alarm_events} WHERE ts > now()",
    },
    {
        "id": "B12", "kind": "B", "owner": "手册片段",
        # P1-7 的正解。不用「所有块都必须有 params」——告警码表 9 块全是 {}，
        # 它们本来就没有可比对参数，那样写会误报 9 行。
        # 判据取「正文里原样出现了库内某个参数名，却没有填进 params」：
        # 这种块是忘填，一旦空着就永久退出 check_doc_conflict 的比对，
        # 而且没有任何报错——冲突检测少一份证据，结论却照常输出。
        # 召回有限：正文用简称（「亮度」而不是「光源亮度设定」）抓不到，见输出说明。
        "desc": "正文提到参数名却没填 params（忘了填就永久退出冲突检测）",
        # 空对象用 jsonb_build_object() 表达，不写 '{}'::jsonb——那个花括号会被
        # 下面的 .format(**tables) 当成占位符，报 Replacement index out of range。
        "sql": """WITH keys AS (
                      SELECT DISTINCT jsonb_object_keys(params) AS k
                      FROM {chunks}
                      WHERE params IS NOT NULL AND params <> jsonb_build_object()
                  )
                  SELECT c.chunk_uid, c.doc_type, c.section, k.k AS mentioned_param
                  FROM {chunks} c
                  JOIN keys k ON position(k.k IN c.content) > 0
                  WHERE c.params IS NULL OR c.params = jsonb_build_object()""",
    },
    # ---------- A 类：pg 源由 DB 保证，价值在换源之后 ----------
    {
        "id": "A1", "kind": "A", "owner": "误判日志",
        "desc": "日志的 device_id 必须存在于设备档案（pg 上由 FK 保证）",
        "sql": """SELECT DISTINCT d.device_id FROM {defect_logs} d
                  LEFT JOIN {devices} x ON x.device_id = d.device_id
                  WHERE x.device_id IS NULL""",
    },
    {
        "id": "A2", "kind": "A", "owner": "标定记录",
        "desc": "标定结论只能是 通过/告警/失败（pg 上由 CHECK 保证）",
        "sql": "SELECT id, result FROM {calibrations} WHERE result NOT IN ('通过','告警','失败')",
    },
    {
        "id": "A3", "kind": "A", "owner": "工单",
        "desc": "同一幂等键同时只能有一张未关闭的单（pg 上由唯一索引保证）",
        "sql": """SELECT idempotency_key, count(*) AS n FROM {work_orders}
                  WHERE status NOT IN ('已完成','已合并','已取消')
                  GROUP BY 1 HAVING count(*) > 1""",
    },
    {
        "id": "A4", "kind": "A", "owner": "误判日志",
        "desc": "verdict 只能是 OK/NG（pg 上由 CHECK 保证）",
        "sql": "SELECT id, verdict FROM {defect_logs} WHERE verdict NOT IN ('OK','NG')",
    },
]

DEFAULT_TABLES = {
    "devices": "devices",
    "defect_logs": "defect_logs",
    "calibrations": "calibrations",
    "light_usage": "light_usage",
    "work_orders": "work_orders",
    "alarm_events": "alarm_events",
    "chunks": "device_manual_chunks",
}

NONEMPTY_TABLES = [
    "devices", "defect_logs", "alarms", "alarm_events",
    "calibrations", "light_usage", "light_brightness_history", "work_orders",
]

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def select_assertions(tables: dict):
    """只保留「引用的表都在 tables 里」的断言，其余明确跳过而不是静默不跑。

    静默跳过最危险：报告上看起来「全绿」，其实一半没检查。
    """
    usable, skipped = [], []
    for item in ASSERTIONS:
        need = set(_PLACEHOLDER.findall(item["sql"]))
        if need <= set(tables):
            usable.append(dict(item, sql=item["sql"].format(**tables)))
        else:
            skipped.append((item["id"], sorted(need - set(tables))))
    return usable, skipped


def fmt_rows(rows: list) -> str:
    if not rows:
        return ""
    shown = []
    for r in rows[:3]:
        shown.append("{" + ", ".join(f"{k}={v}" for k, v in r.items()) + "}")
    more = "" if len(rows) <= 3 else f" …共 {len(rows)} 行"
    return " / ".join(shown) + more


def quarantine_report(dsn: str, table: str = "stg_quarantine"):
    """隔离区报告：返回 [(reason, 条数, 最近时间)]，表不存在返回 None。

    隔离区不该有数据。有，就说明有行被挡在主表外——它不报错、不影响任何查询，
    也不会出现在任何业务统计里，是「安静地错」的典型。
    所以它不能只由装载脚本打印一行，必须由检查脚本主动喊出来。
    """
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT reason, count(*) AS n, max(seen_at) AS latest "
                    f"FROM {table} GROUP BY reason ORDER BY n DESC"
                )
                return cur.fetchall()
    except Exception:
        return None


def run(dsn: str, tables: dict, nonempty: list, expect: str = "", verbose: bool = True,
        only=None):
    """跑断言，返回 (fails, a_fails, results)。供 CLI 和跨源矩阵共用。

    only 是一个断言编号集合，只为「单独验证某一条断言」服务；None 表示全跑。
    """
    usable, skipped = select_assertions(tables)
    if only:
        usable = [a for a in usable if a["id"] in only]
    results = []

    if verbose:
        print(f"目标库：{dsn.rsplit('/', 1)[-1]}   表：{', '.join(sorted(tables.values()))}")
        print(f"断言 {len(usable)} 条" + (f"，跳过 {len(skipped)} 条（缺表）" if skipped else ""))
        print()
        print(f"{'编号':<4} {'类':<2} {'检什么':<52} {'结果':<6} {'坏行'}")
        print("-" * 108)

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        for item in usable:
            cur.execute(item["sql"])
            rows = cur.fetchall()
            results.append((item["id"], item["kind"], not rows, rows))
            if verbose:
                status = "PASS" if not rows else "FAIL"
                print(f"{item['id']:<4} {item['kind']:<2} {item['desc'][:50]:<52} {status:<6} {fmt_rows(rows)}")

        empty_tables = []
        if not only or "B11" in only:
            for table in nonempty:
                try:
                    cur.execute(f"SELECT count(*) AS n FROM {table}")
                except Exception:
                    continue  # 表不存在就不算这条断言的失败
                if cur.fetchone()["n"] == 0:
                    empty_tables.append(table)
            results.append(("B11", "B", not empty_tables,
                            [{"empty_tables": ", ".join(empty_tables)}] if empty_tables else []))
            if verbose:
                status = "PASS" if not empty_tables else "FAIL"
                detail = "" if not empty_tables else ", ".join(empty_tables)
                print(f"B11  B  {'每张业务表都要有数据（空表会冒充「没有异常」）':<52} {status:<6} {detail}")

    fails = [rid for rid, _, ok, _ in results if not ok]
    a_fails = [rid for rid, kind, ok, _ in results if not ok and kind == "A"]
    return fails, a_fails, results


def main(argv=None) -> int:
    dsn = DEFAULT_DSN
    expect = ""
    prefix = ""
    allow_q = False
    only = None
    json_out = ""
    for arg in (argv if argv is not None else sys.argv[1:]):
        if arg.startswith("--dsn="):
            dsn = arg.split("=", 1)[1]
        elif arg.startswith("--expect="):
            expect = arg.split("=", 1)[1]
        elif arg.startswith("--prefix="):
            prefix = arg.split("=", 1)[1]
        elif arg.startswith("--only="):
            only = {x.strip().upper() for x in arg.split("=", 1)[1].split(",") if x.strip()}
        elif arg.startswith("--json="):
            json_out = arg.split("=", 1)[1]
        elif arg == "--allow-quarantine":
            allow_q = True

    if prefix:
        # staging 模式没有手册片段表，把它排除掉让 B12 明确跳过，而不是拼出一个不存在的表名去查
        tables = {k: prefix + v for k, v in DEFAULT_TABLES.items() if k != "chunks"}
    else:
        tables = dict(DEFAULT_TABLES)
    nonempty = [prefix + t for t in NONEMPTY_TABLES] if prefix else NONEMPTY_TABLES

    fails, a_fails, results = run(dsn, tables, nonempty, expect, only=only)

    print("-" * 108)
    print(f"断言 {len(results)} 条：PASS {len(results) - len(fails)} / FAIL {len(fails)}")
    print("※ A 类在 pg 源上由数据库约束保证；换源（staging / CSV / 外部库）后才是主要防线。")

    q = None
    q_fail = False
    if only:
        # 只验某一条断言时不查隔离区：否则「隔离区有 1 条造出来的脏行」
        # 会让 B12 的结果根本看不到，退出码也不是 B12 的结论。
        print("隔离区：本次只验指定断言，跳过")
    else:
        q = quarantine_report(dsn)
        if q is None:
            print("隔离区：这张库里没有 stg_quarantine（该场景不涉及增量装载）")
        elif not q:
            print("隔离区：空")
        else:
            total = sum(r["n"] for r in q)
            print(f"隔离区：{total} 条未进主表")
            for r in q:
                print(f"  - {r['reason']}：{r['n']} 条，最近 {r['latest']}")
            if allow_q:
                print("  （--allow-quarantine 已放行：测试场景故意造的脏行）")
            else:
                q_fail = True

    # 结构化落盘（G6）：让评测报告能把「这轮数据是干净的」写成可引用的证据，
    # 而不是只活在终端输出里、翻不回来。只写库名不写完整 DSN，避免密码进报告。
    if json_out:
        q_detail = None
        if q is not None:
            q_detail = {
                "rows": sum(r["n"] for r in q),
                "reasons": {r["reason"]: r["n"] for r in q},
            }
        payload = {
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db": dsn.rsplit("/", 1)[-1],
            "expect": expect,
            "prefix": prefix,
            "total": len(results),
            "pass": len(results) - len(fails),
            "fail": fails,
            "quarantine": q_detail,
            "status": "ok" if (not fails and not q_fail) else "fail",
        }
        Path(json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"JSON 已写入 {json_out}")

    # 没指定 --expect 时，任何一条红都该让退出码非零。
    # 原来的判断只覆盖了 --expect=clean/dirty 两种，于是「不带参数直接跑」
    # 会在断言红着的情况下返回 0——CI 里等于永远绿。
    if not expect and fails:
        print(f"\n[FAIL] {len(fails)} 条断言红：{', '.join(fails)}")
        return 1
    if expect == "clean" and fails:
        print(f"\n[FAIL] 期望全绿，实际 {len(fails)} 条红：{', '.join(fails)}")
        return 1
    if expect == "dirty" and not fails:
        print("\n[FAIL] 期望抓出脏行，一条都没抓到——断言本身有问题")
        return 1
    if a_fails and not prefix:
        print(f"\n[FAIL] A 类断言在 pg 源上不该红：{', '.join(a_fails)}")
        return 1
    if q_fail:
        print("\n[FAIL] 隔离区非空：有行被挡在主表外。它不会报错，也没人看，"
              "等于把脏数据藏起来了")
        return 1
    print("\n[OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
