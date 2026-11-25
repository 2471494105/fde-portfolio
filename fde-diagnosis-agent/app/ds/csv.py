"""CSV 行源：证明「第二种形态」能挂在同一个接缝上，而不必是数据库。

刻意只做一件事：按主键取一行。不把 SQL 翻成 CSV 查询——那需要给每条 SQL
做特判（WHERE、聚合、JOIN 全都要各写一套），是另一个项目，不是接缝。

一个真实约束写在 capabilities() 里：devices 档案的 log_count / latest_log_at
来自 defect_logs 聚合，CSV 给不了。必须在这里说清楚，让对账脚本跳过它们；
如果返回 None 让上层去比，就会被记成「不一致」——那是假缺陷，最误导人。
"""

import csv
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "data" / "external"

# 这个源能提供的列。写死在这里而不是反射 CSV 表头，是因为「文件里恰好有这列」
# 和「这个源承诺提供这列」不是一回事：客户下次导出的文件可能多一列
# 「备注」，那不代表系统能靠它。
DEVICE_COLUMNS = [
    "device_id",
    "line",
    "station",
    "camera_model",
    "lens_model",
    "light_model",
    "install_date",
    "maintenance_cycle_days",
]


class CsvSource:
    name = "csv"

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DIR
        self._cache = {}  # table -> (columns, {key: row})

    def _load(self, table: str, key_column: str):
        cached = self._cache.get(table)
        if cached is not None:
            return cached
        path = self.data_dir / f"{table}.csv"
        if not path.exists():
            raise FileNotFoundError(f"缺少数据文件：{path}")
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            columns = list(reader.fieldnames or [])
            if key_column not in columns:
                raise ValueError(f"{path.name} 缺少主键列 {key_column!r}，实际列：{columns}")
            rows = {}
            for rec in reader:
                key = (rec.get(key_column) or "").strip()
                if not key:
                    continue  # 空主键的行不该静默进入索引，否则会顶掉真数据
                rows[key] = {k: (v or "").strip() for k, v in rec.items()}
        self._cache[table] = (columns, rows)
        return columns, rows

    def keys(self, table: str, key_column: str = "device_id") -> list:
        return sorted(self._load(table, key_column)[1].keys())

    def get_by_key(
        self, table: str, key: str, key_column: str = "device_id"
    ) -> Optional[dict]:
        rows = self._load(table, key_column)[1]
        row = rows.get(key)
        return dict(row) if row is not None else None

    def health(self) -> dict:
        try:
            self._load("devices", "device_id")
            return {"ok": True, "name": self.name, "dir": str(self.data_dir)}
        except Exception as exc:
            return {"ok": False, "name": self.name, "dir": str(self.data_dir),
                    "reason": str(exc)}

    def capabilities(self) -> list:
        return ["row_lookup", "read_only", "columns:" + ",".join(DEVICE_COLUMNS)]
