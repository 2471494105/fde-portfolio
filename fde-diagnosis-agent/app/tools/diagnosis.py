"""工具 1-4：设备档案 / 误判日志 / 标定记录 / 光源使用。

设计要点：
- 时间窗缺省时以"数据最新时间"为基准倒推 7 天，而不是用 now()。
  因为演示数据是历史区间，用 now() 会查空。
- 误判口径统一为：verdict='NG' AND is_actually_ok=TRUE（过杀）。
"""

from datetime import datetime, timedelta

from app.tools.db import connect

GROUP_EXPR = {
    "hour": "date_trunc('hour', ts)",
    "day": "date_trunc('day', ts)",
    "station": "station",
    "defect_type": "defect_type",
    "batch": "batch",
}


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
            return {"error": f"设备 {device_id} 不存在", "known_devices": known}
        cur.execute("""SELECT count(*) AS n, max(ts) AS m
                       FROM defect_logs WHERE device_id = %s""", (device_id,))
        agg = cur.fetchone()
    return {
        "device_id": row["device_id"],
        "line": row["line"],
        "station": row["station"],
        "camera_model": row["camera_model"],
        "lens_model": row["lens_model"],
        "light_model": row["light_model"],
        "install_date": _fmt(row["install_date"]),
        "maintenance_cycle_days": row["maintenance_cycle_days"],
        "log_count": agg["n"],
        "latest_log_at": _fmt(agg["m"]),
    }


def query_defect_log(device_id: str, time_from: str = "", time_to: str = "",
                     group_by: str = "hour") -> dict:
    if group_by not in GROUP_EXPR:
        return {"error": f"group_by 只支持 {sorted(GROUP_EXPR)}"}

    tf, tt = _parse_ts(time_from), _parse_ts(time_to)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM defect_logs WHERE device_id = %s", (device_id,))
        if cur.fetchone()["n"] == 0:
            return {"error": f"设备 {device_id} 没有误判日志"}

        cur.execute("SELECT max(ts) AS m FROM defect_logs WHERE device_id = %s", (device_id,))
        latest = cur.fetchone()["m"]
        tf = tf or (latest - timedelta(days=7))
        tt = tt or latest

        expr = GROUP_EXPR[group_by]
        cur.execute(f"""
            SELECT {expr} AS key,
                   count(*) AS cnt,
                   count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) AS fp
            FROM defect_logs
            WHERE device_id = %s AND ts >= %s AND ts <= %s
            GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT 50
        """, (device_id, tf, tt))
        groups = cur.fetchall()

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
    return {
        "device_id": device_id,
        "time_from": _fmt(tf),
        "time_to": _fmt(tt),
        "group_by": group_by,
        "total": total,
        "false_positive_count": fp,
        "false_positive_rate": round(fp / total, 4) if total else 0.0,
        "groups": [
            {
                "key": _fmt(g["key"]),
                "count": g["cnt"],
                "false_positive": g["fp"],
                "rate": round(g["fp"] / g["cnt"], 4) if g["cnt"] else 0.0,
            }
            for g in groups[:20]
        ],
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


def query_calibration(device_id: str, limit: int = 5) -> dict:
    limit = max(1, min(int(limit), 20))
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT ts, drift_px, drift_mm, result, operator
                       FROM calibrations WHERE device_id = %s
                       ORDER BY ts DESC LIMIT %s""", (device_id, limit))
        records = cur.fetchall()
        if not records:
            return {"device_id": device_id, "records": [], "message": "该设备无标定记录"}
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

    return {
        "device_id": device_id,
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


def query_light_usage(device_id: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM light_usage WHERE device_id = %s", (device_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"设备 {device_id} 无光源记录"}
        cur.execute("""SELECT ts, value FROM light_brightness_history
                       WHERE device_id = %s ORDER BY ts""", (device_id,))
        hist = cur.fetchall()

    over = row["cumulative_hours"] - row["rated_life_hours"]
    if over > 0:
        estimate = f"已超额定寿命 {over} 小时，亮度衰减显著，误判率通常上升 10%-25%，建议更换光源"
    elif over > -1000:
        estimate = f"接近额定寿命（还差 {-over} 小时），建议安排预防性更换"
    else:
        estimate = f"寿命正常（剩余 {-over} 小时）"

    return {
        "device_id": device_id,
        "light_model": row["light_model"],
        "cumulative_hours": row["cumulative_hours"],
        "rated_life_hours": row["rated_life_hours"],
        "over_life_hours": over,
        "brightness_set": row["brightness_set"],
        "last_replaced_at": _fmt(row["last_replaced_at"]),
        "brightness_history": [{"ts": _fmt(h["ts"]), "value": h["value"]} for h in hist],
        "degradation_estimate": estimate,
    }
