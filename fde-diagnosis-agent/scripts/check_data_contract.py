"""数据断言：查「不该存在的行」。

判据沿用 dbt 的形态：每条断言编成一条 select，只返回坏行；0 行才算通过。
这样判失败不需要额外规则——「有没有行」本身就是答案。

两类断言含义不同，别混着看：

  A 结构类：在 pg 源上由 DB 的 PK / FK / CHECK / 唯一索引保证，跑出来永远 0 行。
            它的价值在换源之后——CSV、外部库、客户自有系统都没有这些约束，
            那时只有断言还在拦。所以 A 类在这里绿，不等于「没事可做」。

  B 业务类：DB 本来就不管（空串、未来时间、跨表一致性、量级合理性）。
            跑出红行才是真发现，也是断言唯一有意义的部分。

用法：
  python scripts/check_data_contract.py --dsn=... --expect=dirty
  python scripts/check_data_contract.py --dsn=... --expect=clean
"""

import sys
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
        "sql": "SELECT device_id, line, station FROM devices WHERE btrim(device_id) = ''",
    },
    {
        "id": "B2", "kind": "B", "owner": "设备档案",
        "desc": "设备号去空格转小写后必须唯一（防 CX-101 / cx-101 各存一份）",
        "sql": """SELECT lower(btrim(device_id)) AS norm_key,
                         count(*) AS n,
                         string_agg(device_id, ', ' ORDER BY device_id) AS ids
                  FROM devices GROUP BY 1 HAVING count(*) > 1""",
    },
    {
        "id": "B3", "kind": "B", "owner": "误判日志",
        "desc": "置信度必须在 0-1",
        "sql": "SELECT id, device_id, confidence FROM defect_logs WHERE confidence < 0 OR confidence > 1",
    },
    {
        "id": "B4", "kind": "B", "owner": "误判日志",
        "desc": "日志时间不能在未来（客户系统时钟错、手工补录写错年份）",
        "sql": "SELECT id, device_id, ts FROM defect_logs WHERE ts > now()",
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
                      FROM devices
                      WHERE station ~ '^\d+-\d+号工位$'
                  )
                  SELECT d.id, d.device_id, d.station AS log_station,
                         r.lo || '-' || r.hi || '号工位' AS archive_range
                  FROM defect_logs d
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
        "sql": "SELECT id, device_id, batch FROM defect_logs WHERE length(btrim(batch)) < 4",
    },
    {
        "id": "B7", "kind": "B", "owner": "标定记录",
        "desc": "像素漂移与毫米漂移必须同号（一正一负说明换算公式或坐标方向错）",
        "sql": """SELECT id, device_id, drift_px, drift_mm FROM calibrations
                  WHERE drift_px <> 0 AND drift_mm <> 0 AND (drift_px * drift_mm) < 0""",
    },
    {
        "id": "B8", "kind": "B", "owner": "光源使用",
        "desc": "累计时长不能超过标称寿命的 3 倍（超了大概率单位错）",
        "sql": """SELECT device_id, cumulative_hours, rated_life_hours FROM light_usage
                  WHERE rated_life_hours <= 0 OR cumulative_hours > rated_life_hours * 3""",
    },
    {
        "id": "B9", "kind": "B", "owner": "工单",
        "desc": "工单的设备号必须存在于设备档案（work_orders 没有外键，能塞进去）",
        "sql": """SELECT w.order_id, w.device_id FROM work_orders w
                  LEFT JOIN devices d ON d.device_id = w.device_id
                  WHERE d.device_id IS NULL""",
    },
    {
        "id": "B10", "kind": "B", "owner": "告警",
        "desc": "告警发生时间不能在未来",
        "sql": "SELECT id, device_id, ts FROM alarm_events WHERE ts > now()",
    },
    # ---------- A 类：pg 源由 DB 保证，价值在换源之后 ----------
    {
        "id": "A1", "kind": "A", "owner": "误判日志",
        "desc": "日志的 device_id 必须存在于设备档案（FK 保证）",
        "sql": """SELECT DISTINCT d.device_id FROM defect_logs d
                  LEFT JOIN devices x ON x.device_id = d.device_id
                  WHERE x.device_id IS NULL""",
    },
    {
        "id": "A2", "kind": "A", "owner": "标定记录",
        "desc": "标定结论只能是 通过/告警/失败（CHECK 保证）",
        "sql": "SELECT id, result FROM calibrations WHERE result NOT IN ('通过','告警','失败')",
    },
    {
        "id": "A3", "kind": "A", "owner": "工单",
        "desc": "同一幂等键同时只能有一张未关闭的单（部分唯一索引保证）",
        "sql": """SELECT idempotency_key, count(*) AS n FROM work_orders
                  WHERE status NOT IN ('已完成','已合并','已取消')
                  GROUP BY 1 HAVING count(*) > 1""",
    },
    {
        "id": "A4", "kind": "A", "owner": "误判日志",
        "desc": "verdict 只能是 OK/NG（CHECK 保证）",
        "sql": "SELECT id, verdict FROM defect_logs WHERE verdict NOT IN ('OK','NG')",
    },
]

# 空表单独处理：它不是一个 SQL 能表达的事，而是「表里有没有数据」。
NONEMPTY_TABLES = [
    "devices", "defect_logs", "alarms", "alarm_events",
    "calibrations", "light_usage", "light_brightness_history", "work_orders",
]


def fmt_rows(rows: list) -> str:
    if not rows:
        return ""
    shown = []
    for r in rows[:3]:
        shown.append("{" + ", ".join(f"{k}={v}" for k, v in r.items()) + "}")
    more = "" if len(rows) <= 3 else f" …共 {len(rows)} 行"
    return " / ".join(shown) + more


def main() -> int:
    dsn = DEFAULT_DSN
    expect = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--dsn="):
            dsn = arg.split("=", 1)[1]
        elif arg.startswith("--expect="):
            expect = arg.split("=", 1)[1]

    db_name = dsn.rsplit("/", 1)[-1]
    print(f"目标库：{db_name}  期望：{expect or '未指定'}")
    print()
    print(f"{'编号':<4} {'类':<2} {'检什么':<52} {'结果':<6} {'坏行'}")
    print("-" * 108)

    fails = []
    a_fails = []

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        for item in ASSERTIONS:
            cur.execute(item["sql"])
            rows = cur.fetchall()
            status = "PASS" if not rows else "FAIL"
            if rows:
                fails.append(item["id"])
                if item["kind"] == "A":
                    a_fails.append(item["id"])
            mark = "" if item["kind"] == "B" else "※"
            print(f"{item['id']:<4} {item['kind']:<2} {item['desc'][:50]:<52} {status:<6} {fmt_rows(rows)}")

        # 空表检查
        empty_tables = []
        for table in NONEMPTY_TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            if cur.fetchone()["n"] == 0:
                empty_tables.append(table)
        status = "PASS" if not empty_tables else "FAIL"
        ids = "B11"
        if empty_tables:
            fails.append(ids)
        print(f"{ids:<4} {'B':<2} {'每张业务表都要有数据（空表会冒充「没有异常」）':<52} "
              f"{status:<6} {', '.join(empty_tables)}")

    print("-" * 108)
    print(f"断言 {len(ASSERTIONS) + 1} 条：PASS {len(ASSERTIONS) + 1 - len(fails)} / FAIL {len(fails)}")
    print("※ 标记为 A 类的断言在 pg 源上由数据库约束保证，换源（CSV / 外部库）后才是主要防线。")

    if expect == "clean" and fails:
        print(f"\n[FAIL] 期望全绿，实际 {len(fails)} 条红：{', '.join(fails)}")
        return 1
    if expect == "dirty" and not fails:
        print("\n[FAIL] 期望抓出脏行，一条都没抓到——断言本身有问题")
        return 1
    if a_fails:
        print(f"\n[FAIL] A 类断言在 pg 源上不该红：{', '.join(a_fails)}")
        return 1
    print("\n[OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
