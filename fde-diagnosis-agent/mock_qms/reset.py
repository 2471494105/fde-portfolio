"""把 work_orders 恢复到确定状态（评测起点隔离）。

为什么必须有这一步
------------------
`work_orders` 是这套系统里唯一会被"被测行为"写脏的表：建单走 HTTP
`POST /api/work-order` → `INSERT INTO work_orders`。而 mock_qms 没有任何
reset / reseed / DELETE 端点，所以每跑一次建单用例，库里就多一张单，
**且下一次跑会读到它**。

两个方向都由它带偏，且都实测发生过：

  假红（干净库）：W2 的期望是 deduped=True，但库里没有同 key 的旧单，
     模型建了一张新单、返回 deduped=False → 判据判失败。那一刻它的行为
     其实是对的（"现场确实没有重复单"）。

  假绿（脏库）：2026-11-19 实测库里挂着 7 张历史工单，其中
     CX-101+light_degradation 有三张、一张仍是"待处理"。这个状态下跑 W1，
     模型第一次调建单就命中旧单返回 deduped=True，**根本没建新单**；
     而 W1 的判据只看回答里有没有 "WO-"，照样判通过。
     那个绿色来自 11-15 的残留，不是这一轮模型做对了。

一手依据（Anthropic《Demystifying evals for AI agents》）：

    Each trial should be "isolated" by starting from a clean environment.
    Unnecessary shared state between runs (leftover files, cached data,
    resource exhaustion) can cause correlated failures ... **Shared state
    can also artificially inflate performance.**

用法
----
    python mock_qms/reset.py           # 清空，打印前后行数
    python mock_qms/reset.py --dry     # 只读，看当前脏不脏

只动 work_orders。devices / defect_log / calibration / light_usage 这些是
只读种子数据，任何行为都不会写它们，清掉反而让评测失去输入。
"""

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"
BACKUP_DIR = Path(__file__).resolve().parent / "backups"


def _count(cur) -> int:
    cur.execute("SELECT count(*) AS n FROM work_orders")
    return cur.fetchone()["n"]


def _open_rows(cur) -> list:
    """列出"还占着幂等键"的行——它们是下一次建单会被 dedup 掉的原因。"""
    cur.execute(
        "SELECT order_id, device_id, root_cause_code, status "
        "FROM work_orders WHERE status NOT IN ('已完成', '已合并', '已取消') "
        "ORDER BY created_at"
    )
    return cur.fetchall()


def reset_work_orders(dry: bool = False, backup: bool = True) -> dict:
    """清空工单表。返回 {before, after, dry, occupying, backup_path}。

    dry=True 只读不写，用来在跑之前看一眼环境脏不脏。
    backup=True 时先把整表导出到 mock_qms/backups/ 再删——这些行是历次评测的
    产物，删掉不可逆，而"环境隔离"本身不需要它们消失得无声无息。
    失败时抛出异常而不是静默返回 0：清不干净就等于这次跑的起点不确定，
    那比直接报错更危险——报告上会是一个"看起来正常"的数。
    """
    backup_path = ""
    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        before = _count(cur)
        occupying = _open_rows(cur)
        if not dry and before:
            if backup:
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = str(BACKUP_DIR / f"work_orders_{stamp}.json")
                cur.execute("SELECT * FROM work_orders ORDER BY created_at")
                rows = cur.fetchall()
                Path(backup_path).write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            cur.execute("DELETE FROM work_orders")
            conn.commit()
        after = before if dry else _count(cur)
    return {
        "before": before,
        "after": after,
        "dry": dry,
        "occupying": occupying,
        "backup_path": backup_path,
    }


def main(argv: list) -> int:
    dry = "--dry" in argv
    try:
        r = reset_work_orders(dry=dry)
    except Exception:
        print("reset 失败，本次评测的起点无法确定：", file=sys.stderr)
        traceback.print_exc()
        return 2

    if r["occupying"]:
        print(f"占用幂等键的未关闭工单 {len(r['occupying'])} 张"
              "（它们会让下一次建单直接 dedup）：")
        for row in r["occupying"]:
            print(f"  {row['order_id']} | {row['device_id']} | "
                  f"{row['root_cause_code']} | {row['status']}")
    else:
        print("没有未关闭的历史工单，起点是干净的。")

    if r["dry"]:
        print(f"\n[dry] work_orders 现有 {r['before']} 张，未改动。")
    else:
        print(f"\nwork_orders: {r['before']} → {r['after']}"
              f"{'（已清空）' if r['before'] else '（本来就是空的）'}")
        if r["backup_path"]:
            print(f"原数据已备份到 {r['backup_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
