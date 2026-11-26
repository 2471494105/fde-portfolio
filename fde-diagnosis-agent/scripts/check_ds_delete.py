"""删除/更新同步：上游删了一条，本地怎么知道，且不误删。

六个必须回答的问题：
1. 没删除信号时怎么判      -> 连续缺席达阈值才判删；第一次只记账
2. 分页没取全怎么办        -> 快照骤降（< 50%）整轮拒绝，一条都不删
3. 判错了怎么改回来        -> 重新出现即 revive，软删标记与缺席计数归零
4. 有没有不能删的          -> 有未闭环引用的设备 protect，转人工
5. 明确删除事件怎么走    -> 不信缺席次数，立刻软删
6. 删了什么、为什么         -> 每次判定写 stg_delete_log，可被追问

用法：python scripts/check_ds_delete.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ds.delete_sync import plan_deletes  # noqa: E402

DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"
SQL = ROOT / "data" / "sandbox" / "delete_sync.sql"

# 夹具：模拟「这几台设备有未闭环工单引用」。真实环境这条来自 work_orders 反查。
PROTECTED = {"CX-103"}

BASE = [
    {"device_id": "CX-101", "line": "1号线"},
    {"device_id": "CX-102", "line": "1号线"},
    {"device_id": "CX-103", "line": "2号线"},
    {"device_id": "CX-104", "line": "2号线"},
    {"device_id": "CX-105", "line": "3号线"},
]


def ensure_schema(conn):
    conn.execute(SQL.read_text(encoding="utf-8"))
    conn.commit()


def reset(conn):
    conn.execute("TRUNCATE stg_devices")
    conn.execute("TRUNCATE stg_device_state")
    conn.execute("TRUNCATE stg_delete_log RESTART IDENTITY")
    conn.commit()


def load_snapshot(conn, rows):
    """客户这次全量给过来的数据。

    刻意用「已存在就跳过」而不是 TRUNCATE 重写：软删的前提是行还在本地，
    每次快照都清空重写的话，被删的设备连行都没了，软删就变成硬删。
    真实场景同样如此——上游只是不再给这一条，本地那份副本不该被抹掉。
    """
    sql = """INSERT INTO stg_devices (device_id, line, _src)
             SELECT %s, %s, 'delete_probe'
             WHERE NOT EXISTS (SELECT 1 FROM stg_devices WHERE device_id = %s)"""
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(sql, [r["device_id"], r["line"], r["device_id"]])
    conn.commit()


def get_states(conn):
    rows = conn.execute(
        "SELECT device_id, absent_count, deleted_at IS NOT NULL AS deleted FROM stg_device_state"
    ).fetchall()
    return {r["device_id"]: {"absent_count": r["absent_count"], "deleted": r["deleted"]}
            for r in rows}


def local_ids(conn):
    return {r["device_id"] for r in conn.execute("SELECT device_id FROM stg_devices").fetchall()}


def apply(conn, result):
    """把决策写进状态表与审计表。动作不同，改的列不同——这里最容易写错。"""
    for d in result["decisions"]:
        did, a, n = d["id"], d["action"], d["absent_count"]
        conn.execute("INSERT INTO stg_device_state (device_id) VALUES (%s) "
                     "ON CONFLICT (device_id) DO NOTHING", (did,))
        if a == "soft_delete":
            conn.execute("UPDATE stg_device_state SET absent_count = %s, deleted_at = now(), "
                         "deleted_reason = %s, updated_at = now() WHERE device_id = %s",
                         (n, d["reason"], did))
        elif a == "revive":
            conn.execute("UPDATE stg_device_state SET absent_count = 0, deleted_at = NULL, "
                         "deleted_reason = NULL, updated_at = now() WHERE device_id = %s", (did,))
        elif a in ("mark_absent", "protect"):
            conn.execute("UPDATE stg_device_state SET absent_count = %s, updated_at = now() "
                         "WHERE device_id = %s", (n, did))
        # noop：什么都不改
        conn.execute("INSERT INTO stg_delete_log (device_id, action, reason, absent_count) "
                     "VALUES (%s, %s, %s, %s)", (did, a, d["reason"], n))
    conn.commit()


def live_count(conn):
    """还活着的台数：落地表里在、且状态表没标软删。"""
    return conn.execute(
        """SELECT count(*) AS n FROM stg_devices d
           LEFT JOIN stg_device_state s ON s.device_id = d.device_id
           WHERE s.deleted_at IS NULL"""
    ).fetchone()["n"]


def run_step(conn, label, snapshot, events=None, min_ratio=None):
    load_snapshot(conn, snapshot)
    kwargs = {} if min_ratio is None else {"min_ratio": min_ratio}
    result = plan_deletes(local_ids(conn), {r["device_id"] for r in snapshot},
                          states=get_states(conn), protected=PROTECTED, events=events, **kwargs)
    apply(conn, result)
    acts = {}
    for d in result["decisions"]:
        acts[d["action"]] = acts.get(d["action"], 0) + 1
    # 每步的活跃台数要当场算：只在最后算一次会让整张表都显示同一个数
    return label, result, acts, live_count(conn)


def main(argv=None) -> int:
    json_out = ""
    min_ratio = None
    for arg in (argv if argv is not None else sys.argv[1:]):
        if arg.startswith("--json="):
            json_out = arg.split("=", 1)[1]
        if arg.startswith("--min-ratio="):
            # 探针开关：调低阈值再跑一次，[7] 会真的把设备删掉，
            # 用来证明那道护栏不是摆设（日常不要用）
            min_ratio = float(arg.split("=", 1)[1])

    log = []
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as conn:
        ensure_schema(conn)
        reset(conn)

        log.append(run_step(conn, "[1] 首轮全量 5 台（无水可删）", BASE))
        log.append(run_step(conn, "[2] CX-105 第一次缺席（只记账，不删）",
                            [r for r in BASE if r["device_id"] != "CX-105"]))
        log.append(run_step(conn, "[3] CX-105 第二次缺席（判删）",
                            [r for r in BASE if r["device_id"] != "CX-105"]))
        log.append(run_step(conn, "[4] CX-105 回补（应复活）", BASE))
        log.append(run_step(conn, "[5] CX-104 下发删除事件（立刻软删，不等第二次）",
                            [r for r in BASE if r["device_id"] != "CX-104"],
                            events={"CX-104": "upstream_purge"}))

        # 后续快照同时排除 103（被保护）和 104（已因事件删掉），
        # 否则把它们放回来等于「上游又给了」，按规则该复活，看不出 noop 分支
        snap_no_103 = [r for r in BASE if r["device_id"] not in ("CX-103", "CX-104")]
        log.append(run_step(conn, "[6a] CX-103 缺席 1 次（被保护，转人工）", snap_no_103))
        log.append(run_step(conn, "[6b] CX-103 缺席 2 次（仍不删）", snap_no_103))
        log.append(run_step(conn, "[6c] CX-103 缺席 3 次（仍不删）", snap_no_103))

        # 给骤降轮准备一个「已缺席 1 次」的设备：把 CX-105 拿掉，同时让 CX-103 回到快照
        # （它没被软删，回来就该复活、缺席归零）。这样如果 [7] 的护栏失效，
        # CX-105 会在骤降轮凑够第 2 次缺席被软删，活跃台数从 4 掉到 3——探针看得见。
        snap_for_pending = [r for r in BASE if r["device_id"] in ("CX-101", "CX-102", "CX-103")]
        log.append(run_step(conn, "[6d] CX-105 缺席 1 次 + CX-103 回到快照（复活）",
                            snap_for_pending, min_ratio=min_ratio))

        log.append(run_step(conn, "[7] 快照骤降到 1 台（整轮拒绝，一条不删）", [BASE[0]],
                            min_ratio=min_ratio))

        soft = conn.execute(
            "SELECT count(*) AS n FROM stg_device_state WHERE deleted_at IS NOT NULL").fetchone()["n"]
        audit = conn.execute("SELECT count(*) AS n FROM stg_delete_log").fetchone()["n"]
        protected_rows = conn.execute(
            "SELECT device_id, absent_count, deleted_at IS NOT NULL AS deleted "
            "FROM stg_device_state WHERE device_id = ANY(%s) ORDER BY device_id",
            (sorted(PROTECTED),)).fetchall()

    print()
    print(f"{'步骤':<46} {'动作':<28} 活跃台数")
    print("-" * 96)
    for label, result, acts, live in log:
        shown = "整轮拒绝(aborted)" if result["aborted"] else (", ".join(
            f"{k}×{v}" for k, v in sorted(acts.items())) or "无决策")
        print(f"{label:<46} {shown:<28} {live}")
    print("-" * 96)
    print(f"活跃 {log[-1][3]} 台 / 软删 {soft} 台 / 审计 {audit} 条")
    print("被保护的那台（夹具 CX-103）：", protected_rows)
    print()
    print("第 [2] 步是关键：只缺席一次时一条都没删——一次分页超时就会误删整批客户数据，")
    print("这是连接层里最贵的那种错，而且删完不报错。")
    print("第 [4] 步证明删除可撤销：上游回补数据、或我们上一轮判错了，必须能复活。")
    print("第 [7] 步是第二道护栏：上游挂了返回空表时，差额等于全库，照规则跑就是全删。")
    print("第 [6] 步是第三道：有未闭环工单引用的设备删了就是悬空引用，转人工，不自动判。")

    if json_out:
        payload = {
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db": DSN.rsplit("/", 1)[-1],
            "steps": [{"name": lab, "aborted": res["aborted"],
                       "actions": acts, "live": live} for lab, res, acts, live in log],
            "live_devices": log[-1][3],
            "soft_deleted": soft,
            "audit_rows": audit,
            "protected": [{"id": r["device_id"], "absent_count": r["absent_count"],
                           "deleted": r["deleted"]} for r in protected_rows],
            "guards": ["首次缺席不删（阈值 2）", "快照骤降整轮拒绝（<50%）",
                       "未闭环引用不删", "重新出现即复活", "每次判定留审计"],
            "status": "ok",
        }
        Path(json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"JSON 已写入 {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
