"""清理 LangGraph 的 checkpoint 表。

为什么要写这个：每轮 invoke 每个节点写一行，59 个会话已经占 2.1 MB。
评测一天跑 3 轮 × 24 条就是 72 个会话，不清理一年能到 GB 级。

	delete from checkpoints
	where thread_id in (
	    select thread_id from checkpoints
	    group by thread_id
	    having max((checkpoint->>'ts')::timestamptz) < now() - interval '7 days'
	);

注意：checkpoints 表没有 created_at 列，时间戳只在 checkpoint->>'ts' 里；
删除必须按顺序打三张表，blobs 和 writes 对 checkpoints 没有 ON DELETE CASCADE。

用法：
  python scripts\\prune_checkpoints.py --dry-run        # 只看会删什么
  python scripts\\prune_checkpoints.py --days 7         # 删 7 天前没动过的会话
  python scripts\\prune_checkpoints.py --keep 200       # 只保留最近 200 个会话
"""

import argparse
import os
import sys

import psycopg

DSN = os.environ.get("FDE_PG_DSN", "postgresql://postgres:postgres@localhost:5432/fde_rag")

# 按最后活动时间挑出要删的 thread
SQL_PICK_BY_DAYS = """
select thread_id, max((checkpoint->>'ts')::timestamptz) as last_ts,
       count(*) as rows
from checkpoints
group by thread_id
having max((checkpoint->>'ts')::timestamptz) < now() - (%s || ' days')::interval
order by last_ts
"""

SQL_PICK_BY_KEEP = """
with ranked as (
    select thread_id, max((checkpoint->>'ts')::timestamptz) as last_ts
    from checkpoints
    group by thread_id
    order by last_ts desc
    offset %s
)
select r.thread_id, r.last_ts,
       (select count(*) from checkpoints c where c.thread_id = r.thread_id) as rows
from ranked r
order by r.last_ts
"""

SQL_STATS = """
select (select count(*) from checkpoints) as ckpt,
       (select count(*) from checkpoint_blobs) as blobs,
       (select count(*) from checkpoint_writes) as writes,
       (select count(distinct thread_id) from checkpoints) as threads
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="删除最后活动早于 N 天的会话")
    ap.add_argument("--keep", type=int, default=None, help="只保留最近 N 个会话")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不删")
    a = ap.parse_args()

    if a.days is None and a.keep is None:
        a.days = 7

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_STATS)
            before = cur.fetchone()
            print(f"清理前：checkpoints={before[0]} blobs={before[1]} "
                  f"writes={before[2]} threads={before[3]}")

            if a.keep is not None:
                cur.execute(SQL_PICK_BY_KEEP, (a.keep,))
            else:
                cur.execute(SQL_PICK_BY_DAYS, (str(a.days),))
            targets = cur.fetchall()

            if not targets:
                print("没有需要清理的会话")
                return

            print(f"将删除 {len(targets)} 个会话（共 {sum(t[2] for t in targets)} 个检查点）：")
            for tid, ts, rows in targets[:15]:
                print(f"  {tid}  最后活动 {ts:%Y-%m-%d %H:%M}  {rows} 行")
            if len(targets) > 15:
                print(f"  … 另有 {len(targets) - 15} 个")

            if a.dry_run:
                print("\n--dry-run：未执行删除")
                return

            tids = [t[0] for t in targets]
            # 必须按序删：blobs / writes 引用 checkpoints，没有级联删除
            cur.execute("delete from checkpoint_writes where thread_id = any(%s)", (tids,))
            n_w = cur.rowcount
            cur.execute("delete from checkpoint_blobs where thread_id = any(%s)", (tids,))
            n_b = cur.rowcount
            cur.execute("delete from checkpoints where thread_id = any(%s)", (tids,))
            n_c = cur.rowcount
            conn.commit()
            print(f"\n已删除：checkpoints {n_c} 行、blobs {n_b} 行、writes {n_w} 行")

            cur.execute(SQL_STATS)
            after = cur.fetchone()
            print(f"清理后：checkpoints={after[0]} blobs={after[1]} "
                  f"writes={after[2]} threads={after[3]}")


if __name__ == "__main__":
    sys.exit(main())
