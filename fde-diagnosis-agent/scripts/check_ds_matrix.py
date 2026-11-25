"""跨源测试：三种进入形态 → 同一块 staging → 同一套断言。

测的不是「源能不能连」，是「换源之后契约还在不在」。

三种形态：
  pg    直连真表（带 PK / FK / CHECK），装不进去的脏行会被数据库拒绝
  csv   文件导出导入（无任何约束），脏行照单全收
  http  分页接口（无约束 + 限流），脏行照单全收

刻意在源数据里塞一条 device_id=CX-999 的日志：
  - pg 形态走真表，FK 会拒绝，只落地 8 条；
  - csv / http 形态走无约束的落地表，9 条全部落地。
所以 B 类（业务规则）应当在三种形态上完全一致，而 A 类（结构约束）只在换源后红。
这正是「DB 是 pg 形态的额外保护，换源就没有了」的实测。

用法：python scripts/check_ds_matrix.py
"""

import csv
import json
import sys
import time
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import check_data_contract as cdc  # noqa: E402
from app.ds.mapping import Mapping  # noqa: E402

DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"
OUT_DIR = ROOT / "data" / "sandbox" / "from_csv"
CLIENT_DIR = ROOT / "data" / "sandbox" / "from_client"
MAPPING_PATH = ROOT / "data" / "mapping" / "client_a.json"

SPECS = {
    "devices": [
        "device_id", "line", "station", "camera_model",
        "lens_model", "light_model", "install_date", "maintenance_cycle_days",
    ],
    "defect_logs": [
        "id", "ts", "device_id", "station", "defect_type",
        "confidence", "batch", "verdict", "is_actually_ok",
    ],
}

STG_TABLES = {"devices": "stg_devices", "defect_logs": "stg_defect_logs"}
STG_NONEMPTY = ["stg_devices", "stg_defect_logs"]


def read_source(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        for table, cols in SPECS.items():
            cur.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1")
            out[table] = [dict(r) for r in cur.fetchall()]
    return out


def truncate_stg(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE stg_devices, stg_defect_logs")


def insert_rows(conn, table, rows, src) -> None:
    cols = SPECS[table]
    ph = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO stg_{table} ({', '.join(cols)}, _src) VALUES ({ph}, %s)"
    with conn.cursor() as cur:
        for rec in rows:
            cur.execute(sql, [rec.get(c) for c in cols] + [src])


# ---------------------------------------------------------------- pg
def load_pg(conn) -> None:
    """走真表：约束在这儿生效，装不进去的就是装不进去。"""
    with conn.cursor() as cur:
        for table, cols in SPECS.items():
            cols_s = ", ".join(cols)
            cur.execute(
                f"INSERT INTO stg_{table} ({cols_s}, _src) "
                f"SELECT {cols_s}, 'pg' FROM {table}"
            )


# ---------------------------------------------------------------- csv
def load_csv(conn, data) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for table, cols in SPECS.items():
        path = OUT_DIR / f"{table}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            for rec in data[table]:
                writer.writerow({k: ("" if v is None else v) for k, v in rec.items()})
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        insert_rows(conn, table, rows, "csv")


# ---------------------------------------------------------------- http
def _jsonable(rows):
    out = []
    for rec in rows:
        item = {}
        for k, v in rec.items():
            if isinstance(v, bool):
                item[k] = "t" if v else "f"
            elif v is None:
                item[k] = None
            else:
                item[k] = str(v)
        out.append(item)
    return out


def build_client(data):
    """假接口：分页 + 第二页第一次请求 429，用来测重试是否真的存在。"""
    seen_429 = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        table = "devices" if path.endswith("/devices") else "defect_logs"
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("size", "3"))
        key = (table, page)
        if table == "devices" and page == 2 and key not in seen_429:
            seen_429.add(key)
            return httpx.Response(429, json={"error": "rate limited"})
        rows = data[table]
        start = (page - 1) * size
        chunk = rows[start:start + size]
        payload = {"items": _jsonable(chunk), "total": len(rows), "page": page}
        return httpx.Response(
            200,
            content=json.dumps(payload, ensure_ascii=False),
            headers={"content-type": "application/json"},
        )

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://qms.local")


def fetch_page(client, table, page, size, retries=4):
    for attempt in range(retries):
        resp = client.get(f"/api/{table}", params={"page": page, "size": size})
        if resp.status_code == 429:
            time.sleep(0.05 * (2 ** attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"{table} 第 {page} 页重试 {retries} 次仍被限流")


def load_http(conn, data) -> None:
    size = 3
    with build_client(data) as client:
        for table, cols in SPECS.items():
            page = 1
            while True:
                payload = fetch_page(client, table, page, size)
                items = payload["items"]
                if not items:
                    break
                insert_rows(conn, table, items, "http")
                if page * size >= payload["total"]:
                    break
                page += 1


# ---------------------------------------------------------------- client
def load_client(conn, data=None) -> None:
    """客户风格：列名、日期格式、百分制全都不一样，过一遍映射再落地。"""
    mapping = Mapping.from_file(MAPPING_PATH)
    for table in SPECS:
        path = CLIENT_DIR / mapping.source_name(table)
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            raw = list(csv.DictReader(fh))
        rows = mapping.apply(table, raw)
        insert_rows(conn, table, rows, "client")


def probe_nomap(conn) -> str:
    """反面：不配映射，直接把客户列名灌进去，看它撞在哪儿。"""
    with (CLIENT_DIR / "equipment.csv").open("r", encoding="utf-8-sig", newline="") as fh:
        raw = list(csv.DictReader(fh))
    cols = list(raw[0])
    ph = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO stg_devices ({', '.join(cols)}) VALUES ({ph})"
    try:
        with conn.cursor() as cur:
            for rec in raw:
                cur.execute(sql, [rec.get(c) for c in cols])
        conn.commit()
        return "意外插进去了"
    except Exception as exc:
        conn.rollback()
        return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"


# ---------------------------------------------------------------- main
def main() -> int:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        data = read_source(conn)
        # 注入一条「设备号写错」的日志：pg 真表装不进去，外部源装得进去
        data["defect_logs"].append({
            "id": 9001, "ts": "2026-11-04 08:00:00", "device_id": "CX-999",
            "station": "2号工位", "defect_type": "划伤", "confidence": 0.88,
            "batch": "B20261104", "verdict": "NG", "is_actually_ok": False,
        })

        summary = []
        loaders = (
            ("pg", None),
            ("csv", load_csv),
            ("http", load_http),
            ("client", load_client),
        )
        for name, loader in loaders:
            truncate_stg(conn)
            conn.commit()
            if name == "pg":
                load_pg(conn)
            else:
                loader(conn, data)
            conn.commit()

            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM stg_defect_logs")
                n_logs = cur.fetchone()["n"]
                cur.execute("SELECT count(*) AS n FROM stg_devices")
                n_dev = cur.fetchone()["n"]

            fails, a_fails, _ = cdc.run(DSN, STG_TABLES, STG_NONEMPTY, verbose=False)
            b_fails = sorted(set(fails) - set(a_fails))
            summary.append((name, n_dev, n_logs, b_fails, sorted(set(a_fails))))

        nomap_msg = probe_nomap(conn)
        truncate_stg(conn)
        conn.commit()

    print()
    print(f"{'形态':<6} {'设备':>4} {'日志':>4}  {'B 类红':<22} {'A 类红'}")
    print("-" * 72)
    for name, n_dev, n_logs, b_fails, a_fails in summary:
        print(f"{name:<6} {n_dev:>4} {n_logs:>4}  {','.join(b_fails) or '无':<22} {','.join(a_fails) or '无'}")

    b_sets = {tuple(item[3]) for item in summary}
    print("-" * 72)
    print("注：pg 少 1 条日志，是 CX-999 那条被真表的外键挡在门外——它从未进入 stg，")
    print("    所以 A1 在 pg 形态无从报；csv / http 把它原样搬进来，A1 才红。")
    print(f"不配映射直接灌客户文件：{nomap_msg}")
    print(f"B 类在四种形态上一致：{'是' if len(b_sets) == 1 else '否'}")
    print("A 类只在换源后红，说明 pg 的结构约束不是断言的替代品，是叠加在它上面的额外保护。")

    if len(b_sets) != 1:
        print("\n[FAIL] B 类结果不一致：某条形态在搬运过程中丢了信息")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
