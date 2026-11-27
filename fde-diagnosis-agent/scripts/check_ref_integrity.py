"""判据 M：跨表引用一致性（同一个编号在两张表里对不上，就没有人能 join）。

为什么补这一条
--------------
前三类（归属、字段、隔离区）与报警闭环都在看「一张表内部对不对」。客户真正会踩的
另一种坑是**跨表**：缺陷记录说设备是 `CX-999`，主数据里根本没有这台机器——单看
`biz_defect_record` 每一列都"填了"，join 一次才发现指向的是一个不存在的东西。

判什么、不判什么（这一条的重点在"不判"）
--------------------------------------
**判**（有唯一事实、对不上就是脏）：见 `REFS`
- 缺陷记录 `equip_no` → `md_device.equip_no`（设备编号是客户自己的编码，没有第二套叫法）
- 维修单 `rec_id` → 缺陷记录 `rec_id`（同一张库的主键引用）

**不判**（已经确认是两套叫法，字面对齐会造出假红）：见 `NOT_COMPARED`
- 工单 `station_no` → 主数据 `station_no`：工单写「1号工位」、主数据写「产线A-01」，
  60 行**全部**对不上；这跟 T8 判过的「别名表兜底」是同一件事，映射要产线一起给，
  后端不猜。
- 缺陷 `lot_no` → 检验批 `lot_no`：`B20260401` 与 `LOT-A-20260301-01` 两种编码，
  302 行里 0 命中。
这两条被**显式写进台账的 `not_compared`**，附原因——比"默默不查"强：客户问
「为什么不查这个」时，判据自己写着答案。

编号格式 `defect_equip#9001` / `repair_defect#RO-...`：与其他判据同一套「前缀 + 定位键」，
以后若要进「常见问题目录」，在 `gov_issue_catalog.targets.prefixes` 里加上前缀即可，
明细页不用改。

用法：
    python scripts/check_ref_integrity.py
    python scripts/check_ref_integrity.py --no-record
    python scripts/check_ref_integrity.py --dsn=...      # 换观测面
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import runlog  # noqa: E402

CHECK_ID = "ref_integrity"
CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"

# (引用名, 子表, 子表列, 主表, 主表列, 定位键列)
REFS = [
    ("defect_equip", "biz_defect_record", "equip_no", "md_device", "equip_no", "rec_id"),
    ("repair_defect", "biz_repair_order", "rec_id", "biz_defect_record", "rec_id", "ro_no"),
]

# 不比对：已确认是两套叫法/两套编码，字面对齐只会造假红
NOT_COMPARED = [
    ("biz_work_order.station_no → md_station.station_no",
     "工单写「1号工位」、主数据写「产线A-01」，60 行全部对不上：两套叫法，映射要产线一起给，"
     "后端不猜（同 T8 判过的别名表兜底）"),
    ("biz_defect_record.lot_no → biz_inspection_lot.lot_no",
     "批次编码两套（B20260401 vs LOT-A-20260301-01），302 行里 0 命中：口径不同，不是脏"),
]


def judge_one(conn, name: str, child: str, col: str, parent: str, pcol: str, key: str) -> dict:
    sql = (
        f"SELECT count(*) AS total,"
        f" count(*) FILTER (WHERE {col} IS NULL) AS nulls,"
        f" count(*) FILTER (WHERE {col} IS NOT NULL AND {col}::text NOT IN"
        f"   (SELECT {pcol}::text FROM {parent})) AS orphans"
        f" FROM {child}"
    )
    stat = dict(conn.execute(sql).fetchone())
    ids = []
    if stat["orphans"]:
        rows = conn.execute(
            f"SELECT {key} AS k, {col} AS v FROM {child}"
            f" WHERE {col} IS NOT NULL AND {col}::text NOT IN (SELECT {pcol}::text FROM {parent})"
            f" ORDER BY {key}").fetchall()
        ids = [f"{name}#{r['k']}" for r in rows]
    return {"name": name, "left": f"{child}.{col}", "right": f"{parent}.{pcol}",
            "rows": stat["total"], "nulls": stat["nulls"], "orphans": stat["orphans"],
            "ids": ids}


def main(argv: list[str]) -> int:
    dsn, json_out, record_run = CLIENT_DSN, None, True
    for a in argv:
        if a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--json="):
            json_out = a.split("=", 1)[1]
        elif a == "--no-record":
            record_run = False
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2

    t0 = time.perf_counter()
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            refs = [judge_one(conn, *spec) for spec in REFS]
    except Exception as e:
        print(f"[error] 取数失败（{dsn}）：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    red_rows = sum(r["orphans"] for r in refs)
    total_ids = [i for r in refs for i in r["ids"]]
    print(f"观测面 {dsn.rsplit('/', 1)[-1]}，比对 {len(refs)} 条引用")
    for r in refs:
        tag = "FAIL" if r["orphans"] else "OK"
        extra = f"，编号 {r['ids'][:3]}" if r["ids"] else ""
        print(f"[{tag}] {r['name']:14s} {r['left']} → {r['right']}"
              f" 行 {r['rows']} · 空 {r['nulls']} · 孤儿 {r['orphans']}{extra}")
    for pair, why in NOT_COMPARED:
        print(f"[跳过] {pair} —— {why}")
    print(f"[{'FAIL' if red_rows else 'OK':4s}] 引用一致性：{red_rows} 行指向不存在的键")

    status = "fail" if red_rows else "ok"
    summary = {"dsn": dsn, "status": status, "red_rows": red_rows, "ids": total_ids,
               "refs": [{k: v for k, v in r.items() if k != "ids"} for r in refs],
               "not_compared": [{"pair": p, "why": w} for p, w in NOT_COMPARED]}

    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json → {out}")

    if record_run:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        details = {
            "red_metric": "orphan_rows",
            "refs": [{k: v for k, v in r.items() if k != "ids"} for r in refs],
            "ids": total_ids,
            "not_compared": [{"pair": p, "why": w} for p, w in NOT_COMPARED],
        }
        try:
            run_id = runlog.record(CHECK_ID, status, red_rows, duration_ms,
                                   dataset=f"fde_client @ {dsn.rsplit('/', 1)[-1]}",
                                   details=details, dsn=dsn)
            print(f"运行台账 → run_id={run_id}（{status}，孤儿 {red_rows} 行，{duration_ms} ms）")
        except Exception as e:
            print(f"[warn] 运行台账没写进去：{type(e).__name__}: {e}", file=sys.stderr)

    return 0 if status == "ok" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("检查脚本自身崩了（这一轮结果不可当结论）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)
