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

# 「未闭环」的口径必须和 mock_qms/reset.py 逐字一致：那边用它算「谁还占着幂等键」，
# 这里用它算「谁不能被删」。同一件事不能有两套定义，否则一处宽一处严，
# 会出现「建单能建、设备却被删掉」这种自相矛盾的现场。
OPEN_STATUS_EXCLUDE = ("已完成", "已合并", "已取消")

# 夹具工单：造出「一台有未闭环工单、一台只有已闭环工单」两种样本。
# 保护集本身不再由常量决定，而是从 work_orders 反查——
# 这两条只是把客户库里会有的行提前造出来，取数走和真实环境同一条 SQL。
ORDER_FIXTURES = [
    {"order_id": "WO-PROBE-103-OPEN", "device_id": "CX-103", "status": "待处理"},
    {"order_id": "WO-PROBE-102-CLOSED", "device_id": "CX-102", "status": "已完成"},
]

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


def ensure_order_fixtures(conn):
    """造两条工单：一条未闭环（该保护）、一条已闭环（不该保护）。

    两条都要有，否则「反查口径」只测了一半：只造未闭环的，你永远不知道
    代码有没有把已闭环的也算进去——那样保护会无限扩大，最后什么都不敢删。
    """
    conn.execute("DELETE FROM work_orders WHERE order_id LIKE 'WO-PROBE-%'")
    for f in ORDER_FIXTURES:
        conn.execute(
            "INSERT INTO work_orders (order_id, device_id, root_cause, root_cause_code, "
            "severity, actions, evidence_refs, idempotency_key, status, assignee_group) "
            "VALUES (%s, %s, '夹具：验证保护集反查', 'PROBE', 'medium', '无', '[]', %s, %s, '夹具组')",
            (f["order_id"], f["device_id"], f"probe-{f['order_id']}", f["status"]),
        )
    conn.commit()


def open_order_devices(conn):
    """保护集的真实来源：从 work_orders 反查「有未闭环工单的设备」。

    接客户库时只换 DSN，代码不用动——这就是把 `PROTECTED = {"CX-103"}`
    这种常量替掉的意义。常量只对某一个沙箱有效，换台机器就是错的。
    """
    rows = conn.execute(
        "SELECT DISTINCT device_id FROM work_orders WHERE NOT (status = ANY(%s))",
        (list(OPEN_STATUS_EXCLUDE),),
    ).fetchall()
    return {r["device_id"] for r in rows}


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
        "SELECT device_id, absent_count, deleted_at IS NOT NULL AS deleted, protected "
        "FROM stg_device_state"
    ).fetchall()
    return {r["device_id"]: {"absent_count": r["absent_count"], "deleted": r["deleted"],
                             "protected": r["protected"]}
            for r in rows}


def protected_ids(conn):
    """同步 `protected` 列，并返回此刻的保护集。

    这列是**状态**，不是历史：历史在 `stg_delete_log` 里（每次 protect 都留痕）。
    所以每次跑都把它对齐成「此刻有未闭环工单」的真相，工单闭环后自动变回 false。

    反过来说：如果把「表里标记过」并进保护集，保护就只增不减——一台设备被
    保护过一次就永远删不掉，跑久了所有设备都被保护过，删除功能等于没有。
    这比「保护突然消失」更隐蔽：后者会有人问，前者没有任何症状。
    """
    open_ids = open_order_devices(conn)
    ids = list(open_ids)
    conn.execute(
        "UPDATE stg_device_state SET protected = (device_id = ANY(%s)), updated_at = now() "
        "WHERE protected <> (device_id = ANY(%s))",
        (ids, ids),
    )
    conn.commit()
    return open_ids


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
            # 刻意不清 protected：保护的理由是「有未写完的工单」，设备重新出现并不
            # 说明工单闭环了。自动解开会让保护名存实亡。
            conn.execute("UPDATE stg_device_state SET absent_count = 0, deleted_at = NULL, "
                         "deleted_reason = NULL, updated_at = now() WHERE device_id = %s", (did,))
        elif a == "mark_absent":
            conn.execute("UPDATE stg_device_state SET absent_count = %s, updated_at = now() "
                         "WHERE device_id = %s", (n, did))
        elif a == "protect":
            # 把判定落到表里：否则「受保护」只活在脚本常量里，查库永远是空
            conn.execute("UPDATE stg_device_state SET absent_count = %s, protected = true, "
                         "updated_at = now() WHERE device_id = %s", (n, did))
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
    # 先同步保护标记，再读状态：否则 states 里带的是上一步的旧标记
    protected = protected_ids(conn)
    result = plan_deletes(local_ids(conn), {r["device_id"] for r in snapshot},
                          states=get_states(conn), protected=protected,
                          events=events, **kwargs)
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
        ensure_order_fixtures(conn)
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

        # 最后两组是「信号冲突」：这两步以前会直接删，是 11-26 下午补的
        snap_no_104 = [r for r in BASE if r["device_id"] != "CX-104"]
        log.append(run_step(conn, "[8] CX-101 仍在快照，却又收到删除事件（矛盾，不删）",
                            snap_no_104, events={"CX-101": "upstream_purge"}))
        snap_no_103_104 = [r for r in BASE if r["device_id"] not in ("CX-103", "CX-104")]
        log.append(run_step(conn, "[9] CX-103 收到删除事件，但它有未闭环工单（转人工，不删）",
                            snap_no_103_104, events={"CX-103": "upstream_purge"}))

        soft = conn.execute(
            "SELECT count(*) AS n FROM stg_device_state WHERE deleted_at IS NOT NULL").fetchone()["n"]
        audit = conn.execute("SELECT count(*) AS n FROM stg_delete_log").fetchone()["n"]
        # 保护集从工单反查，不再来自常量——这里查的就是「代码此刻会保护谁」
        protected_source = sorted(open_order_devices(conn))
        protected_rows = conn.execute(
            "SELECT device_id, absent_count, deleted_at IS NOT NULL AS deleted, protected "
            "FROM stg_device_state WHERE device_id = ANY(%s) ORDER BY device_id",
            (protected_source,)).fetchall()

    print()
    print(f"{'步骤':<46} {'动作':<28} 活跃台数")
    print("-" * 96)
    for label, result, acts, live in log:
        shown = "整轮拒绝(aborted)" if result["aborted"] else (", ".join(
            f"{k}×{v}" for k, v in sorted(acts.items())) or "无决策")
        print(f"{label:<46} {shown:<28} {live}")
    print("-" * 96)
    print(f"活跃 {log[-1][3]} 台 / 软删 {soft} 台 / 审计 {audit} 条")
    # 骤降轮的证据只在中间态：后续 [8] 会把快照补回来、被删的设备复活，
    # 最终台数看不出护栏开没开。所以把那一刻单独打出来，别靠人翻中间行。
    drop_step = next((x for x in log if x[0].startswith("[7]")), None)
    if drop_step:
        print(f"骤降轮 [7] 当时的活跃台数：{drop_step[3]} 台"
              f"（正常模式应为 4；低于 4 说明护栏没拦住）")
    print("被保护的那台（从 work_orders 反查未闭环工单）：", protected_rows)
    print(f"反查到的保护集：{protected_source}"
          f"（口径：status NOT IN {OPEN_STATUS_EXCLUDE}）")
    print()
    print("第 [2] 步是关键：只缺席一次时一条都没删——一次分页超时就会误删整批客户数据，")
    print("这是连接层里最贵的那种错，而且删完不报错。")
    print("第 [4] 步证明删除可撤销：上游回补数据、或我们上一轮判错了，必须能复活。")
    print("第 [7] 步是第二道护栏：上游挂了返回空表时，差额等于全库，照规则跑就是全删。")
    print("第 [6] 步是第三道：有未闭环工单引用的设备删了就是悬空引用，转人工，不自动判。")
    print("第 [8][9] 步是补的：删除事件不能删掉「这次还在快照里」的设备，也不能越过未闭环工单。")

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
                           "deleted": r["deleted"], "marked": r["protected"]}
                          for r in protected_rows],
            # 保护集从哪来，要写进产物：读报告的人得能判断「这个保护是配的还是查的」
            "protected_source": {
                "from": "work_orders 反查",
                "rule": f"status NOT IN {list(OPEN_STATUS_EXCLUDE)}",
                "ids": protected_source,
            },
            "guards": ["首次缺席不删（阈值 2）", "快照骤降整轮拒绝（<50%）",
                       "未闭环引用不删（优先于删除事件）", "信号矛盾不删",
                       "重新出现即复活", "每次判定留审计"],
            "status": "ok",
        }
        Path(json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"JSON 已写入 {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
