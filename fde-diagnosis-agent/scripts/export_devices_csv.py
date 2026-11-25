"""把 devices 表导出成 CSV，模拟「客户只肯给导出的文件、不给库账号」。

为什么不用 psql 的 \\copy：
1. Windows 下 \\copy 的路径与编码容易出岔，python 能显式指定 utf-8-sig；
2. 这个脚本本身能带走——真实项目里常需要「从库里导一份给外部系统用」，
   而这个动作一旦做第二次，就不该每次手敲。

输出位置刻意放在 data/external/ 而不是 data/seed/：
data/seed 是「数据库初态」，参与 seed_hash；external 是「从别处拿来的数据」，
不该混进那条轴。
"""

import csv
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "external" / "devices.csv"
DSN = os.environ.get("FDE_PG_DSN", "postgresql://postgres:postgres@localhost:5432/fde_rag")

# 只导 devices 自己的列。log_count / latest_log_at 是从 defect_logs 聚合出来的，
# 不属于档案，导出时不该带——带了就等于把「聚合结果」当「原始数据」存，
# 以后日志一变，这份快照就悄悄过期。
COLUMNS = [
    "device_id",
    "line",
    "station",
    "camera_model",
    "lens_model",
    "light_model",
    "install_date",
    "maintenance_cycle_days",
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sql = "SELECT " + ", ".join(COLUMNS) + " FROM devices ORDER BY device_id"
    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    with OUT.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            rec = dict(row)
            if hasattr(rec["install_date"], "isoformat"):
                rec["install_date"] = rec["install_date"].isoformat()
            writer.writerow(rec)

    print(f"写出 {len(rows)} 行 -> {OUT}")


if __name__ == "__main__":
    main()
