"""工具 1-4：设备档案 / 误判日志 / 标定记录 / 光源使用。

设计要点：
- 时间窗缺省时以"数据最新时间"为基准倒推 7 天，而不是用 now()。
  因为演示数据是历史区间，用 now() 会查空。
- 误判口径统一为：verdict='NG' AND is_actually_ok=TRUE（过杀）。
"""

from datetime import datetime, timedelta

from app.ds.facts import FACT_KEYS
from app.tools.db import connect
from app.tools.hints import HINT

# 事实层的租户。设备档案的每个属性都从 `facts.device_fact_current` 里找回自己的
# 那一行，把 `id` 带回去——这是 L4 回指的唯一入口。
#
# 为什么不是"查不到就算了"：`devices` 是源端口径的当前值，`facts` 是我方口径的
# 历史。两者在当前维度必须逐键一致（`check_facts` 第 [6] 步每轮验）。所以查不到
# 只可能是两类事：事实层没同步这台设备，或者租户写错了。两种都该露出来
# （`fact_missing`），不能静默返回一个没有 id 的档案。
FACTS_TENANT = "client_a"

GROUP_EXPR = {
    "hour": "date_trunc('hour', ts)",
    "day": "date_trunc('day', ts)",
    "station": "station",
    "defect_type": "defect_type",
    "batch": "batch",
}


def _parse_dims(group_by):
    """把 group_by 拆成维度列表；含非法维度或为空返回 None。

    2026-11-19 起支持逗号分隔的多维（"station,batch"）：A3 为了看多个维度把
    12 步预算吃满，根因是一次调用只能聚合一个维度。抽成纯函数是为了能不起
    DB 就测参数解析这一段。
    """
    dims = [g.strip() for g in str(group_by or "hour").split(",") if g.strip()]
    if not dims or any(g not in GROUP_EXPR for g in dims):
        return None
    return list(dict.fromkeys(dims))  # 去重并保持顺序


def _parse_ts(value):
    if not value:
        return None
    s = str(value).strip().replace("T", " ").split("+")[0].rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fmt(v):
    return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else v


def query_device_profile(device_id: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT device_id FROM devices ORDER BY device_id")
            known = [r["device_id"] for r in cur.fetchall()]
            # state 是给调用方判「这是查无，还是服务挂了」用的。
            # 只给 error 字符串时，上游只能靠文案猜——而 query_defect_log 的 error
            # 同时用于参数校验失败，猜错就会把「参数写错」记成「库里没有」，
            # 于是拒答依据本身是假的。工具自己声明状态，调用方就不用猜。
            return {"error": f"设备 {device_id} 不存在", "state": "device_not_found",
                    "known_devices": known, "hint": HINT["device_not_found"]}
        cur.execute("""SELECT count(*) AS n, max(ts) AS m
                       FROM defect_logs WHERE device_id = %s""", (device_id,))
        agg = cur.fetchone()
        # 每个属性在事实层的那一行。一次查全，别在返回时逐键再查一遍。
        #
        # 查的是表而不是视图 device_fact_current：视图就是
        # `SELECT ... FROM facts.device_fact WHERE valid_to IS NULL`（data/facts.sql
        # 第 50–54 行），两者同义，但视图不带 valid_to 列——而回指判据要能回答
        # 「这条记录在结论时刻还有效吗」，答案就是 valid_to 是不是 NULL。
        # 只拿 id 不拿 valid_to，「当时有效」只能靠"它来自 current 视图"这句话，
        # 判据就退化成"信工具自己的声明"。视图口径若变了，这里要同步。
        cur.execute("""SELECT fact_key, id, valid_from, valid_to, value
                       FROM facts.device_fact
                       WHERE tenant = %s AND entity_id = %s AND valid_to IS NULL""",
                    (FACTS_TENANT, device_id))
        fact_rows = {r["fact_key"]: r for r in cur.fetchall()}
    # 回指清单：调用方（nodes → state.fact_refs → eval）靠它回答
    # 「这条结论依据的是哪条事实、什么时候生效」。
    # value 一并带上：判据要能逐字比对 claim 里的值，只给 id 不够——
    # id 可能指向一条已经关闭的历史行，也可能指向另一个值。
    fact_refs = [
        {"fact_key": k, "fact_id": fact_rows[k]["id"],
         "valid_from": _fmt(fact_rows[k]["valid_from"]),
         # 非 None = 这条记录在工具调用时已经关闭，不能用来回指「现在」的事实。
         # 正常情况下这里是空的（查询条件就是 valid_to IS NULL）；
         # 字段留着是为了让判据能验，而不是让判据相信。
         "valid_to": _fmt(fact_rows[k]["valid_to"]),
         "value": fact_rows[k]["value"]}
        for k in FACT_KEYS if k in fact_rows
    ]
    return {
        "device_id": row["device_id"],
        "state": "found",
        "line": row["line"],
        "station": row["station"],
        "camera_model": row["camera_model"],
        "lens_model": row["lens_model"],
        "light_model": row["light_model"],
        "install_date": _fmt(row["install_date"]),
        "maintenance_cycle_days": row["maintenance_cycle_days"],
        "log_count": agg["n"],
        "latest_log_at": _fmt(agg["m"]),
        "fact_refs": fact_refs,
        # 事实层没覆盖的键。非空 = 这台设备的部分属性没有历史，
        # 那些属性的结论不可能被回指——露出来，别让人以为都盖住了。
        "fact_missing": [k for k in FACT_KEYS if k not in fact_rows],
    }


def query_defect_log(device_id: str, time_from: str = "", time_to: str = "",
                     group_by: str = "hour") -> dict:
    """误判日志按维度聚合。

    group_by 支持逗号分隔的多个维度（例如 "station,batch"），一次调用返回多组聚合。
    2026-11-19 加：A3 用例为了看 4 个维度、再换时间窗重验 3 个维度，一共调了 7 次
    本工具，把 12 步预算吃满（1045 那次 budget_exhausted）。维度数是"一次能问多少"
    的问题，不该由模型用轮次去拼（Anthropic《Writing effective tools》consolidate）。
    """
    dims = _parse_dims(group_by)
    if dims is None:
        # bad_args 和 no_defect_log 必须分开：一个是模型参数写错（该改参数再查），
        # 一个是库里确实没有（该拒答）。都用 error 字符串时上游分不出来。
        return {"error": (f"group_by 只支持 {sorted(GROUP_EXPR)}；"
                          "要同时看多个维度请用英文逗号连接，例如 station,batch"),
                "state": "bad_args", "hint": HINT["bad_args"]}

    tf, tt = _parse_ts(time_from), _parse_ts(time_to)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM defect_logs WHERE device_id = %s", (device_id,))
        if cur.fetchone()["n"] == 0:
            return {"error": f"设备 {device_id} 没有误判日志", "state": "no_defect_log",
                    "device_id": device_id, "hint": HINT["no_defect_log"]}

        cur.execute("SELECT max(ts) AS m FROM defect_logs WHERE device_id = %s", (device_id,))
        latest = cur.fetchone()["m"]
        tf = tf or (latest - timedelta(days=7))
        tt = tt or latest

        agg = {}
        for d in dims:
            expr = GROUP_EXPR[d]
            cur.execute(f"""
                SELECT {expr} AS key,
                       count(*) AS cnt,
                       count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) AS fp
                FROM defect_logs
                WHERE device_id = %s AND ts >= %s AND ts <= %s
                GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT 50
            """, (device_id, tf, tt))
            rows = cur.fetchall()
            agg[d] = [
                {
                    "key": _fmt(r["key"]),
                    "count": r["cnt"],
                    "false_positive": r["fp"],
                    "rate": round(r["fp"] / r["cnt"], 4) if r["cnt"] else 0.0,
                }
                for r in rows[:20]
            ]

        cur.execute("""
            SELECT count(*) AS cnt,
                   count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) AS fp
            FROM defect_logs
            WHERE device_id = %s AND ts >= %s AND ts <= %s
        """, (device_id, tf, tt))
        tot = cur.fetchone()

        cur.execute("""
            SELECT ts, station, defect_type, confidence, batch
            FROM defect_logs
            WHERE device_id = %s AND ts >= %s AND ts <= %s
              AND verdict = 'NG' AND is_actually_ok
            ORDER BY confidence DESC LIMIT 5
        """, (device_id, tf, tt))
        samples = cur.fetchall()

    total = tot["cnt"] or 0
    fp = tot["fp"] or 0
    out = {
        "device_id": device_id,
        "state": "found",
        "time_from": _fmt(tf),
        "time_to": _fmt(tt),
        "group_by": ",".join(dims),
        "total": total,
        "false_positive_count": fp,
        "false_positive_rate": round(fp / total, 4) if total else 0.0,
        # groups 保持旧形状（等于第一个维度），旧消费者与旧 evidence_refs 不必改
        "groups": agg[dims[0]],
        "samples": [
            {
                "ts": _fmt(s["ts"]),
                "station": s["station"],
                "defect_type": s["defect_type"],
                "confidence": s["confidence"],
                "batch": s["batch"],
            }
            for s in samples
        ],
    }
    if len(dims) > 1:
        # 多维时额外给分维度聚合；单维不加这个键，避免无谓胀大响应
        out["by_group"] = agg
    return out


def query_calibration(device_id: str, limit: int = 5,
                      time_from: str = "", time_to: str = "") -> dict:
    limit = max(1, min(int(limit), 20))

    # 显式给了时间就必须生效。解析不了直接拒，不静默退回"最近 N 条"——
    # 静默忽略参数，调用方会以为按时间过滤了，实际拿的是全时段（范围放大的一种）。
    tf = tt = None
    if str(time_from or "").strip():
        tf = _parse_ts(time_from)
        if tf is None:
            return {"state": "bad_args", "time_from": time_from,
                    "error": f"time_from 无法解析：{time_from}",
                    "hint": "时间格式用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"}
    if str(time_to or "").strip():
        tt = _parse_ts(time_to)
        if tt is None:
            return {"state": "bad_args", "time_to": time_to,
                    "error": f"time_to 无法解析：{time_to}",
                    "hint": "时间格式用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"}
    if tf and tt and tf > tt:
        return {"state": "bad_args", "error": "time_from 晚于 time_to",
                "hint": "时间窗要正着给：time_from <= time_to"}

    with connect() as conn, conn.cursor() as cur:
        sql = """SELECT ts, drift_px, drift_mm, result, operator
                 FROM calibrations WHERE device_id = %s"""
        args = [device_id]
        if tf:
            sql += " AND ts >= %s"
            args.append(tf)
        if tt:
            sql += " AND ts <= %s"
            args.append(tt)
        sql += " ORDER BY ts DESC LIMIT %s"
        args.append(limit)
        cur.execute(sql, args)
        records = cur.fetchall()

        if not records:
            # 区分「这台设备压根没标定过」和「有记录、但给的时间窗内没有」——
            # 两种情况下一步动作完全不同，混成一个 no_calibration 等于让模型瞎猜。
            cur.execute("SELECT count(*) AS n FROM calibrations WHERE device_id = %s",
                        (device_id,))
            if int(cur.fetchone()["n"] or 0) > 0:
                return {"device_id": device_id, "records": [],
                        "state": "no_calibration_in_range",
                        "window": {"time_from": time_from or None,
                                   "time_to": time_to or None},
                        "message": "该设备有标定记录，但指定时间窗内没有",
                        "hint": "放宽 time_from / time_to，或换一段时间重查"}
            return {"device_id": device_id, "records": [], "state": "no_calibration",
                    "message": "该设备无标定记录",
                    "hint": HINT["no_calibration"]}

        cur.execute("SELECT max(ts) AS m FROM defect_logs WHERE device_id = %s", (device_id,))
        latest = cur.fetchone()["m"]

    drifts = [r["drift_mm"] for r in reversed(records)]
    if len(drifts) >= 2:
        if drifts[-1] > drifts[-2] * 1.5:
            trend = "明显上升"
        elif drifts[-1] > drifts[0]:
            trend = "缓慢上升"
        else:
            trend = "平稳"
    else:
        trend = "样本不足"

    out = {
        "device_id": device_id,
        "state": "found",
        "records": [
            {
                "ts": _fmt(r["ts"]),
                "drift_px": r["drift_px"],
                "drift_mm": r["drift_mm"],
                "result": r["result"],
                "operator": r["operator"],
            }
            for r in records
        ],
        "last_calibration_at": _fmt(records[0]["ts"]),
        "days_since_last": (latest - records[0]["ts"]).days if latest else None,
        "drift_trend": trend,
    }
    # 实际生效的时间窗随结果回来：模型看不见窗口，就会把「最近 5 条」当成
    # 「这段时间的全部记录」——跟不传参数时一模一样。
    out["window"] = {"time_from": time_from or None, "time_to": time_to or None}
    return out


def _life_fields(cumulative: int, rated: int):
    """把累计点亮时长与额定寿命换算成两个方向明确的字段。

    对外只给非负数：说"超了多少"看 over_life_hours，说"还剩多少"看
    remaining_hours，各自在另一个方向上是 0。这样字段名和数值方向永远一致，
    不会出现 over_life_hours = -1585 这种读起来就自相矛盾的值。

    定性判断（estimate）**只写判断，不复述数字**：
    数字已经在两个字段里各有一份，文案再写一遍就成了第二个数据源，
    而且符号极容易反过来——上一版是 over_life_hours = -1585 配
    "寿命正常（剩余 1585 小时）"，模型照文案说 1585、ref 指字段里的 -1585，
    判据一比就判"引用写错"。
    """
    over = cumulative - rated
    over_hours = over if over > 0 else 0
    remaining = -over if over < 0 else 0
    if over > 0:
        estimate = "已超额定寿命，亮度衰减显著，误判率通常上升 10%-25%，建议更换光源"
    elif over > -1000:
        estimate = "接近额定寿命，建议安排预防性更换"
    else:
        estimate = "寿命正常"
    return over_hours, remaining, estimate


def query_light_usage(device_id: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM light_usage WHERE device_id = %s", (device_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"设备 {device_id} 无光源记录", "state": "no_light_record",
                    "device_id": device_id, "hint": HINT["no_light_record"]}
        cur.execute("""SELECT ts, value FROM light_brightness_history
                       WHERE device_id = %s ORDER BY ts""", (device_id,))
        hist = cur.fetchall()

    over_hours, remaining, estimate = _life_fields(
        row["cumulative_hours"], row["rated_life_hours"])

    return {
        "device_id": device_id,
        "state": "found",
        "light_model": row["light_model"],
        "cumulative_hours": row["cumulative_hours"],
        "rated_life_hours": row["rated_life_hours"],
        "over_life_hours": over_hours,
        "remaining_hours": remaining,
        "brightness_set": row["brightness_set"],
        "last_replaced_at": _fmt(row["last_replaced_at"]),
        "brightness_history": [{"ts": _fmt(h["ts"]), "value": h["value"]} for h in hist],
        "degradation_estimate": estimate,
    }
