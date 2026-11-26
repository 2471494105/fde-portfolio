"""增量水位：只灌「上次之后」的新数据，而不是每晚全表重传。

六个必须回答的问题，脚本逐个验：
1. 水位存哪        -> stg_watermark 表（我方元数据，可以有 PK）
2. 怎么不重        -> 两道防线：插入与水位推进同一个事务（崩了一起重滚）；
                      装载前按主键过滤已存在的行（幂等，客户重传整批也不重复）
3. 怎么不满停      -> 水位只按「不超过当前时间」的记录推进。
                      源里一条 2027 年的脏时间如果参与取 max，水位跳到 2027，
                      之后所有真实数据都被判定为「已灌过」，增量静默停摆。
                      这条数据照常进隔离区（B4 断言会抓它），但不许它定义水位。
4. 同时间戳怎么办  -> 水位做成 (时间戳, 主键) 复合，同一秒进来的多条只要有更大的
                      主键就算新。第 [6] 步验证它从「漏 2 条」变成「补 2 条」。
5. 边界在哪        -> 复合水位要求主键单调递增。客户重传一条更小的主键（补录旧单）
                      会漏，第 [8] 步把这条残留边界测出来，而不是假装没有；
                      第 [9] 步证明 `overlap`（水位往回退一段）能把它补齐，
                      第 [10] 步证明重叠区全是旧行时水位不会往回退。
6. 用哪个钟        -> 源端过滤 / 本地水位 / 业务时间必须是同一个「钟」，且显式登记。
                      GitHub 的 since 按 committer 过滤、author 可早到 2025-12-15（K-3），
                      本地若记 author 时间，整条会被判「已灌过」而静默丢弃。
                      第 [12] 步核对登记与真实表结构，第 [13] 步造一条业务时间早于水位的行，
                      证明它真的会被漏掉——「我说我按发生时刻筛」，得有一次实跑作证。

用法：python scripts/check_ds_incremental.py
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ds.incremental import plan  # noqa: E402
from app.ds.time_axis import TIME_AXIS, axis_issues  # noqa: E402

DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"
STG = "stg_defect_logs"
COLS = [
    "id", "ts", "device_id", "station", "defect_type",
    "confidence", "batch", "verdict", "is_actually_ok",
]

# 时间轴口径登记在 app/ds/time_axis.py：源端过滤 / 本地水位 / 业务时间三个字段。
# plan 的 ts_key 必须从这里取，不许在调用点另写一个字面值——登记与行为同源，
# 否则「改了登记没改调用」这种事机器也看不出来。
AXIS = TIME_AXIS["defect_logs"]
TS_KEY = AXIS["business_time_field"]

# 源数据放内存里，不碰真表（真表有外键，插不进 CX-999）
BASE = [
    {"id": 1, "ts": "2026-11-01 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.91, "batch": "B20261101", "verdict": "NG", "is_actually_ok": False},
    {"id": 2, "ts": "2026-11-01 09:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.88, "batch": "B20261101", "verdict": "NG", "is_actually_ok": False},
    {"id": 3, "ts": "2026-11-01 10:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.75, "batch": "B20261101", "verdict": "OK", "is_actually_ok": True},
    {"id": 4, "ts": "2027-01-01 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.90, "batch": "B20261101", "verdict": "NG", "is_actually_ok": False},
    {"id": 5, "ts": "2026-11-02 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 1.50, "batch": "B20261102", "verdict": "NG", "is_actually_ok": False},
    {"id": 6, "ts": "2026-11-02 09:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": -0.10, "batch": "B20261102", "verdict": "OK", "is_actually_ok": True},
    {"id": 7, "ts": "2026-11-02 10:00:00", "device_id": "CX-101", "station": "4号线", "defect_type": "划伤", "confidence": 0.92, "batch": "B20261102", "verdict": "NG", "is_actually_ok": False},
    {"id": 8, "ts": "2026-11-03 08:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.80, "batch": "", "verdict": "OK", "is_actually_ok": True},
    {"id": 9, "ts": "2026-11-03 09:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.81, "batch": "A", "verdict": "OK", "is_actually_ok": True},
    {"id": 9001, "ts": "2026-11-04 08:00:00", "device_id": "CX-999", "station": "2号工位", "defect_type": "划伤", "confidence": 0.88, "batch": "B20261104", "verdict": "NG", "is_actually_ok": False},
]


def _ts(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def reset(conn):
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {STG}")
        cur.execute("TRUNCATE stg_watermark")
        cur.execute("TRUNCATE stg_quarantine")
    conn.commit()


def count_rows(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {STG}")
        return cur.fetchone()["n"]


def fmt_mark(mark):
    """水位统一显示成 `时间戳 #主键`，避免第一行是 int、第二行是 str 这类不一致。"""
    if mark is None:
        return "（空）"
    if isinstance(mark, tuple):
        return f"{mark[0]:%Y-%m-%d %H:%M:%S} #{mark[1]}"
    return f"{mark:%Y-%m-%d %H:%M:%S} #（无主键）"


def get_mark(conn):
    """返回 (时间戳, 主键) 或 None。主键为空时退化成只比时间戳。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mark, mark_pk FROM stg_watermark WHERE table_name = 'defect_logs'"
        )
        row = cur.fetchone()
    if not row or not row["mark"]:
        return None
    return (_ts(row["mark"]), row["mark_pk"])


def put_mark(conn, mark, seen):
    """mark 是 (时间戳, 主键) 二元组。"""
    ts, pk = mark
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO stg_watermark (table_name, src, mark, mark_pk, rows_seen, updated_at)
               VALUES ('defect_logs', 'demo', %s, %s, %s, now())
               ON CONFLICT (table_name) DO UPDATE
               SET mark = EXCLUDED.mark,
                   mark_pk = EXCLUDED.mark_pk,
                   rows_seen = stg_watermark.rows_seen + EXCLUDED.rows_seen,
                   updated_at = now()""",
            (ts, None if pk is None else str(pk), seen),
        )


# 重叠窗口取多大是运营口径：按时间倒退（怕迟到很久的补录）还是按行数倒退（怕大批量）。
# 这里 1 天只为把机制测出来，**不是生产建议值**——理由与删除阈值同款，见契约第九节。
OVERLAP_WINDOW = timedelta(days=1)
# 探针专用窗口：证明「重叠区全是旧行时水位不倒退」。故意与上一值不同，它不是口径。
PROBE_OVERLAP = timedelta(days=30)


def overlap_policy():
    """报告里写出去的窗口必须等于代码真在用的那个。

    原来 JSON 里写死字符串 `"overlap": "1 day（第 [9] 步）"`：改常量时没人会记得
    改这行字，读报告的人就按错窗口理解。现在由常量生成。
    """
    return {
        "window_seconds": int(OVERLAP_WINDOW.total_seconds()),
        "window_days": OVERLAP_WINDOW.total_seconds() / 86400,
        "mode": "按时间倒退（捞回主键更小的补录单）",
        "basis": "没有运营依据，1 天只为把机制测出来",
    }


# 隔离区的自指问题：本脚本开头 TRUNCATE stg_quarantine，跑完又只留 1 条固定夹具，
# 于是 data_health 那条「比上次多就红」的判据永远只拿到 delta=0——护栏写在了代码里，
# 但从来没有被输入触发过。第 [11] 步专门造一次 +1 再回收，让那条判据每轮都有活的输入，
# 而不是靠人手工把旧 JSON 改小去验一次。
QUARANTINE_PROBE_ID = 9030
QUARANTINE_PROBE = {
    "id": QUARANTINE_PROBE_ID,
    "ts": "2030-01-01 08:00:00",
    "device_id": "CX-PROBE",
    "station": "2号工位",
    "defect_type": "划伤",
    "confidence": 0.90,
    "batch": "B_PROBE",
    "verdict": "NG",
    "is_actually_ok": False,
}


def count_quarantine(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM stg_quarantine")
        return cur.fetchone()["n"]


def drop_probe_rows(conn):
    """只删自己插的那一行，不 TRUNCATE——检查不该顺手清掉别人的隔离记录。"""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM stg_quarantine WHERE src_table = 'defect_logs' AND row_id = %s",
            (QUARANTINE_PROBE_ID,),
        )
    conn.commit()


def quarantine_probe_verdict(before, after, restored):
    """三个数全部来自真查库，一个都不是声明的。

    返回 (ok, 说明)。data_health 里有「隔离区比上次多就红」的判据，但本项目每轮
    都会把隔离区 TRUNCATE 成恰好 1 条固定夹具，delta 恒为 0——判据在，输入不在。
    探针造一次 +1 再收回，等于每轮给这条判据喂一次真实的输入。
    """
    if after - before != 1:
        return False, (
            f"探针没让隔离区涨 1 条（{before} → {after}）："
            "隔离区上涨已经不可观测，data_health 那条判据等于没验过"
        )
    if restored != before:
        return False, (
            f"探针回收后剩 {restored} 条、原为 {before} 条："
            "检查污染了它自己要检查的对象"
        )
    return True, f"隔离区 {before} → {after}（+1）→ {restored}：上涨可观测，且能收回"


# 时间轴探针：一条「业务时间早于水位、入库时间晚于水位」的假记录。
# 按登记的钟（ts）判，它必然被判「已灌过」；只有代码读错了钟，它才会被灌进主表。
# 这是 K-3 在本地可复现的形态——错读钟不报错，只是少灌/多灌。
TIME_AXIS_PROBE_ID = 9029
TIME_AXIS_PROBE = {
    "id": TIME_AXIS_PROBE_ID,
    "ts": "2026-01-01 00:00:00",           # 业务时间：远早于水位 → 本该被漏掉
    "_loaded_at": "2026-11-26 00:00:00",   # 入库时间：晚于水位 → 错读钟就会把它灌进来
    "device_id": "CX-PROBE",
    "station": "2号工位",
    "defect_type": "划伤",
    "confidence": 0.90,
    "batch": "B_PROBE",
    "verdict": "NG",
    "is_actually_ok": False,
}


def table_columns(conn, table):
    """真实列名。PostgreSQL 里未加引号的标识符一律小写，所以这里不用再归一。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = %s""",
            (table,),
        )
        return [r["column_name"] for r in cur.fetchall()]


def time_axis_registry(conn):
    """核对登记本身：三个字段的名字、互相对应关系、以及它们是否真在表里。

    这一层不跑数据，只比「登记」与「表结构」。名字写错不会让任何东西报错，
    只会让口径变成一句空话——所以缺列即红。
    """
    cols = table_columns(conn, STG)
    issues = axis_issues(AXIS, cols)
    return {
        "registry": AXIS,
        "table": STG,
        "table_cols": cols,
        "plan_ts_key": TS_KEY,
        "static_issues": issues,
        "ok": not issues,
    }


def has_row(conn, row_id):
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 AS n FROM {STG} WHERE id = %s", (row_id,))
        return cur.fetchone() is not None


def run_time_axis_probe(conn):
    """跑一次「必须被漏掉」的探针，跑完不论结果都删掉自己那一行。

    判据三个数全来自真查库：新增条数、探针是否落进主表、清理后是否回到原值。
    """
    before = count_rows(conn)
    load(conn, [TIME_AXIS_PROBE])
    after = count_rows(conn)
    rows_added = after - before
    landed = has_row(conn, TIME_AXIS_PROBE_ID)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {STG} WHERE id = %s", (TIME_AXIS_PROBE_ID,))
    conn.commit()
    left = count_rows(conn)
    left_behind = left != before

    ok = rows_added == 0 and not landed and not left_behind
    if rows_added != 0:
        note = (
            f"探针被灌进了 {rows_added} 条：代码读的不是业务时间字段（{TS_KEY}）。"
            "业务时间早于水位的行本该被判已灌过——这正是 K-3 那种静默丢，"
            "换成真源就是「整批补录单永远进不来」"
        )
    elif left_behind:
        note = (
            f"探针清理后主表剩 {left} 行、原为 {before} 行："
            "检查污染了自己要检查的对象"
        )
    else:
        note = (
            f"探针业务时间 2026-01-01 早于水位，按 {TS_KEY} 判应漏 → 实测新增 0 条、"
            f"主表 {before} 行未变，探针行已清理"
        )
    return {
        "row_id": TIME_AXIS_PROBE_ID,
        "before": before,
        "after": after,
        "rows_added": rows_added,
        "expected_rows_added": 0,
        "landed": landed,
        "left": left,
        "left_behind": left_behind,
        "plan_ts_key": TS_KEY,
        "ok": ok,
        "note": note,
    }


def load(conn, rows, fail_after_insert=False, overlap=None):
    """返回 (本次新增条数, 水位, 本次隔离条数)。插入与水位推进同事务。

    谁算新数据交给 app.ds.incremental.plan（有单测）；这里补上 plan 不知道的一半：
    主表里已经有什么。plan 只按水位判断，客户重传整批时会再吐一遍已灌过的行，
    所以装载前按主键过滤。这就是「plan 负责谁该看，落库负责谁是新的」。
    """
    mark = get_mark(conn)
    good, bad, new_mark = plan(rows, mark, ts_key=TS_KEY, overlap=overlap)

    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {STG}")
        existing = {r["id"] for r in cur.fetchall()}

    fresh = [rec for rec in good if rec["id"] not in existing]

    ph = ", ".join(["%s"] * len(COLS))
    sql = f"INSERT INTO {STG} ({', '.join(COLS)}, _src) VALUES ({ph}, 'incr')"
    with conn.cursor() as cur:
        for rec in bad:
            cur.execute(
                """INSERT INTO stg_quarantine (src_table, row_id, reason, payload)
                   VALUES ('defect_logs', %s, 'ts_in_future', %s::jsonb)
                   ON CONFLICT (src_table, row_id) DO NOTHING""",
                (rec["id"], json.dumps(rec, ensure_ascii=False)),
            )
        for rec in fresh:
            cur.execute(sql, [rec.get(c) for c in COLS])
        if fail_after_insert:
            raise RuntimeError("模拟装载中途崩溃")
        # 只要 plan 认定有新行就推进水位——哪怕这些行全在主表里（已灌过）。
        # 否则「整批重传」会让水位原地不动，下次还要重读同一段。
        if good:
            put_mark(conn, new_mark, len(fresh))

    conn.commit()
    return len(fresh), new_mark, len(bad)


def main(argv=None) -> int:
    json_out = ""
    for arg in (argv if argv is not None else sys.argv[1:]):
        if arg.startswith("--json="):
            json_out = arg.split("=", 1)[1]
    extra_a = [
        {"id": 9010, "ts": "2026-11-05 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.87, "batch": "B20261105", "verdict": "NG", "is_actually_ok": False},
        {"id": 9011, "ts": "2026-11-05 09:00:00", "device_id": "CX-103", "station": "2号工位", "defect_type": "异物", "confidence": 0.66, "batch": "B20261105", "verdict": "NG", "is_actually_ok": False},
    ]
    extra_b = [
        {"id": 9020, "ts": "2026-11-06 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.83, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
    ]
    # 与 extra_b 同一时间戳、主键更大的两条：复合水位要能认出来
    same_ts = [
        {"id": 9021, "ts": "2026-11-06 08:00:00", "device_id": "CX-102", "station": "2号工位", "defect_type": "污点", "confidence": 0.79, "batch": "B20261106", "verdict": "OK", "is_actually_ok": True},
        {"id": 9022, "ts": "2026-11-06 08:00:00", "device_id": "CX-104", "station": "2号工位", "defect_type": "划伤", "confidence": 0.85, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
    ]
    # 同一时间戳、主键更小的补录单：复合水位认不出来，这是它的残留边界
    late_small = [
        {"id": 9019, "ts": "2026-11-06 08:00:00", "device_id": "CX-101", "station": "2号工位", "defect_type": "划伤", "confidence": 0.84, "batch": "B20261106", "verdict": "NG", "is_actually_ok": False},
    ]

    log = []
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        reset(conn)

        n, m, f = load(conn, BASE)
        log.append(("[1] 首次装载（水位为空，全量）", n, f, count_rows(conn), m))

        n, m, f = load(conn, BASE)
        log.append(("[2] 原样重跑（应 0 行）", n, f, count_rows(conn), m))

        rows3 = BASE + extra_a
        n, m, f = load(conn, rows3)
        log.append(("[3] 追加 2 条新日志", n, f, count_rows(conn), m))

        rows4 = rows3 + extra_b
        try:
            load(conn, rows4, fail_after_insert=True)
            crashed = "没崩"
        except RuntimeError:
            conn.rollback()
            crashed = "崩溃，已回滚"
        log.append((f"[4] 装载中途崩溃（{crashed}）", 0, 0, count_rows(conn), get_mark(conn)))

        n, m, f = load(conn, rows4)
        log.append(("[5] 崩溃后重跑（应只补 1 条）", n, f, count_rows(conn), m))

        n, m, f = load(conn, rows4 + same_ts)
        log.append(("[6] 同时间戳 + 更大主键（应补 2 条）", n, f, count_rows(conn), m))

        n, m, f = load(conn, rows4 + same_ts)
        log.append(("[7] 原样重跑（整批重传，应 0 条且不重复）", n, f, count_rows(conn), m))

        n, m, f = load(conn, rows4 + same_ts + late_small)
        log.append(("[8] 同时间戳 + 更小主键（不带窗口，预期漏 1）", n, f, count_rows(conn), m))

        n, m, f = load(conn, rows4 + same_ts + late_small, overlap=OVERLAP_WINDOW)
        log.append((f"[9] 开 {OVERLAP_WINDOW.total_seconds() / 86400:g} 天重叠窗口重跑"
                    f"（应补上那 1 条）", n, f, count_rows(conn), m))

        n, m, f = load(conn, BASE, overlap=PROBE_OVERLAP)
        log.append(("[10] 重叠区全是旧行（应 0 且水位不倒退）", n, f, count_rows(conn), m))

        # [11] 隔离区上涨探针。三个数都真查库，不是算出来的：
        # 造一次 +1 → 收回 → 还原值必须回到原样，否则检查污染了自己要检查的对象。
        q_before = count_quarantine(conn)
        load(conn, [QUARANTINE_PROBE])
        q_after = count_quarantine(conn)
        drop_probe_rows(conn)
        q_restored = count_quarantine(conn)
        probe_ok, probe_note = quarantine_probe_verdict(q_before, q_after, q_restored)
        log.append((f"[11] 隔离区上涨探针（{q_before}→{q_after}→{q_restored}）",
                    0, q_after - q_before, count_rows(conn), get_mark(conn)))

        # [12] 时间轴口径登记：源端过滤 / 本地水位 / 业务时间三个字段互相说得通吗。
        #      比的是「登记」与真实表结构，不跑数据——名字写错本身不会让任何东西报错。
        axis = time_axis_registry(conn)
        log.append(("[12] 时间轴口径登记（源端 / 水位 / 业务时间三字段）",
                    0, 0, count_rows(conn), get_mark(conn)))

        # [13] 时间轴探针：造一条业务时间早于水位的行，它必须被判「已灌过」。
        #      代码若读成 _loaded_at 这类入库时间，这行就会被灌进主表——K-3 的错法当场显形。
        ax_probe = run_time_axis_probe(conn)
        log.append((f"[13] 时间轴探针（业务时间早于水位，应漏；实测新增 {ax_probe['rows_added']}）",
                    ax_probe["rows_added"], 0, count_rows(conn), get_mark(conn)))

        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS total, count(DISTINCT id) AS uniq FROM {STG}")
            r = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM stg_quarantine")
            q = cur.fetchone()["n"]

    print()
    print(f"{'步骤':<40} {'新增':>4} {'隔离':>4} {'累计':>4}  水位")
    print("-" * 100)
    for name, n, f, total, mark in log:
        print(f"{name:<40} {n:>4} {f:>4} {total:>4}  {fmt_mark(mark)}")
    print("-" * 100)
    print(f"总行数 {r['total']} / 不同 id {r['uniq']}，"
          f"{'无重复' if r['total'] == r['uniq'] else '有重复'}")
    print(f"隔离区 {q} 条：重复跑不会涨（主键 + ON CONFLICT DO NOTHING）。")
    print("「隔离」那一列就是 id=4 那条 2027 年的脏数据：它没进主表，所以既不会把水位带到未来，")
    print("也不会因为「永远大于水位」而每晚被重复灌一次。")
    print("第 [6] 步是这次修的：水位改成 (时间戳, 主键) 复合后，同一秒进来的多条不再被整体漏掉。")
    print("第 [8] 步是残留边界：复合水位要求主键单调递增，客户补录一条更小的主键仍会漏。")
    print("第 [9] 步把这条边界补掉：水位往回退 1 天取候选，重叠区靠落库幂等去重，代价是每次多读一小段。")
    print("第 [10] 步守住代价的另一头：重叠区全是旧行时 max 会比原水位更早，水位不许因此倒退。")
    print("第 [11] 步是给隔离区那条判据喂一次真实输入：本脚本开头会 TRUNCATE 隔离区，")
    print("  跑完只留 1 条固定夹具，所以 data_health 的 delta 恒为 0——护栏写着了，但没被触发过。")
    print("第 [12] 步核对时间轴三字段（源端过滤 / 本地水位 / 业务时间）的登记与真实表结构：")
    print("  名字写错不会让任何东西报错，只会让「口径」变成一句空话——所以缺列即红。")
    print("第 [13] 步是 K-3 的本地形态：业务时间早于水位的行必须被漏掉（新增 0 条）；")
    print("  若代码读成 _loaded_at 这类入库时间，这行会被灌进主表——静默丢变成看得见，且跑完自清理。")

    # 结构化落盘（G6）。known_gap 一起写：报告里不能只有「增量跑通了」，
    # 还得带上「它在什么情况下会漏」——否则这份证据比没有更容易被误读。
    if json_out:
        payload = {
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db": DSN.rsplit("/", 1)[-1],
            "steps": [
                {"name": name, "added": n, "quarantine": f, "total": total,
                 "mark": fmt_mark(mark)}
                for name, n, f, total, mark in log
            ],
            "final_rows": r["total"],
            "unique_ids": r["uniq"],
            "duplicated": r["total"] != r["uniq"],
            "quarantine_rows": q,
            # 隔离区「涨了会被发现」这条判据的实证：三个数都来自真查库。
            # 缺它时 data_health 会红——没有反例的护栏，等于没有护栏。
            "quarantine_probe": {
                "row_id": QUARANTINE_PROBE_ID,
                "before": q_before,
                "after": q_after,
                "delta": q_after - q_before,
                "restored": q_restored,
                "ok": probe_ok,
                "note": probe_note,
            },
            "overlap": f"{overlap_policy()['window_days']:g} 天（第 [9] 步）",
            "policy": overlap_policy(),
            # 交叉验证的实证：不带窗口漏几条、带窗口补几条。数字来自实跑，
            # 不是声明——窗口若被改成 0 或写错，这两个数会相等，判据立刻红。
            "overlap_evidence": {
                "leaked_without_window": log[7][1],     # [8]
                "recovered_with_window": log[8][1],     # [9]
                "probe_window_seconds": int(PROBE_OVERLAP.total_seconds()),
            },
            # 时间轴：登记（静态核对）+ 探针（实跑证据）。两件事分开写，
            # 因为「名字对」和「钟对」是两种失败——前者靠表结构，后者只能靠造一条行去看。
            "time_axis": {
                **axis,
                "static_ok": axis["ok"],
                "probe": ax_probe,
                "ok": axis["ok"] and ax_probe["ok"],
            },
            "known_gap": "不给 overlap 时，同时间戳 + 更小主键后到仍会漏（[8] 验证）；"
                         "给 overlap=1 天可补齐（[9] 验证）；重叠区全是旧行时水位不倒退（[10] 验证）",
            "status": "ok" if (r["total"] == r["uniq"] and probe_ok
                               and axis["ok"] and ax_probe["ok"]) else "fail",
        }
        Path(json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"JSON 已写入 {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
