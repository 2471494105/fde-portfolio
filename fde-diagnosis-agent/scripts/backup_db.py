#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C1 备份：把客户库导成一份「可校验」的 dump + 一份 meta。

为什么这算治理而不是运维杂活
--------------------------
判据只回答「这一遍跑的时候有没有违约行」。客户问「那你们改坏了怎么退回去」时，
现在没有任何东西能答——`scripts/` 里只有一次性 `migrate_idem_key.py`。
备份的价值不在文件本身，而在**能证明这份文件真能还原**：所以 dump 旁边带 sha256、
pg_dump 版本、六张关键表的行数与整表指纹，交给 `verify_backup.py` 做还原校核。

边界（写进文档，不靠口头）：
- 只读源库：不锁表、不改任何数据；一致性由 pg_dump 自己的快照保证（不是业务级一致性）。
- meta 里**不落密码**：DSN 只保留 host / port / db。
- 不提供「直接覆盖客户库」的还原入口（见 `restore_db.py`，默认只还原到校核库）。
- 不做增量 / WAL 归档：这是演示级「能退回去」，不是生产级 PITR。

用法：
    python -X utf8 scripts/backup_db.py
    python -X utf8 scripts/backup_db.py --out=out/backup --keep=10
    python -X utf8 scripts/backup_db.py --dsn=postgresql://...

退出码：0 成功 / 2 自身出错（找不到 pg_dump、导出失败、meta 写不出）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
OUT_DIR = ROOT / "out" / "backup"

# 六张关键表：演示里所有数字（407 / 362 / 5 / 40 / 60）都从这些表出来，备份必须覆盖。
TABLES = [
    "biz_work_order",
    "biz_defect_record",
    "biz_alarm",
    "biz_repair_order",
    "md_station",
    "md_device",
]

# 整表指纹：与 demo_closeout.fingerprint 同口径（SELECT * → json(sort_keys, default=str) → sha256[:16]）。
FINGERPRINTS = [
    ("biz_work_order", "wo_no"),
    ("biz_defect_record", "rec_id"),
]


def find_pg_bin(name: str) -> str:
    """找 pg_dump / pg_restore：PATH 优先，其次 env，最后探测常见安装目录。

    找不到就报错，不退回「假装备份成功」。
    """
    env = os.environ.get(name.upper(), "")  # PG_DUMP / PG_RESTORE
    if env and Path(env).is_file():
        return env
    found = shutil.which(name)
    if found:
        return found
    cands = [
        Path(r"E:\POSTGRESQL\bin") / f"{name}.exe",
        *(sorted(Path(r"C:\Program Files\PostgreSQL").glob(f"*/bin/{name}.exe"))
          if Path(r"C:\Program Files\PostgreSQL").is_dir() else []),
    ]
    for c in cands:
        if Path(c).is_file():
            return str(c)
    raise RuntimeError(f"找不到 {name}：装 PostgreSQL 客户端或设 {name.upper()} 环境变量")


def parse_dsn(dsn: str) -> dict:
    u = urlsplit(dsn)
    if not u.hostname or not u.path:
        raise RuntimeError(f"DSN 不完整（要 postgresql://user:pw@host:port/db）：{dsn[:40]}…")
    return {
        "host": u.hostname,
        "port": str(u.port or 5432),
        "user": u.username or "postgres",
        "password": u.password or "",
        "db": u.path.lstrip("/"),
    }


def masked(cfg: dict) -> dict:
    """meta 里只留不含密码的三项。"""
    return {"host": cfg["host"], "port": int(cfg["port"]), "db": cfg["db"]}


def env_for(cfg: dict) -> dict:
    env = dict(os.environ)
    if cfg["password"]:
        env["PGPASSWORD"] = cfg["password"]
    return env


def snapshot(dsn: str) -> dict:
    """六表行数 + 两张表的整表指纹；表不存在直接报错，不静默跳过。"""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name = ANY(%s)",
                (TABLES,),
            )
            have = {r[0] for r in cur.fetchall()}
            missing = [t for t in TABLES if t not in have]
            if missing:
                raise RuntimeError(f"源库缺表：{','.join(missing)}（备份做不成，不当成功）")
            counts = {}
            for t in TABLES:
                cur.execute(f'SELECT count(*) FROM "{t}"')
                counts[t] = int(cur.fetchone()[0])
            fps = {}
            for table, key in FINGERPRINTS:
                cur.execute(f'SELECT * FROM "{table}" ORDER BY "{key}"')
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
                fps[table] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {"counts": counts, "fingerprints": fps}


def backup(dsn: str = CLIENT_DSN, out_dir: Path = OUT_DIR) -> dict:
    """导出一份 dump 并写 meta；返回 meta（调用方据此断言）。"""
    cfg = parse_dsn(dsn)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump = out_dir / f"{cfg['db']}_{stamp}.dump"

    before = snapshot(dsn)
    p = subprocess.run(
        [find_pg_bin("pg_dump"), "-h", cfg["host"], "-p", cfg["port"], "-U", cfg["user"],
         "-d", cfg["db"], "-Fc", "-f", str(dump)],
        env=env_for(cfg), capture_output=True, text=True, encoding="utf-8",
    )
    if p.returncode != 0 or not dump.exists() or dump.stat().st_size == 0:
        raise RuntimeError(f"pg_dump 失败 exit={p.returncode}：{(p.stderr or p.stdout)[-500:]}")

    data = dump.read_bytes()
    ver = subprocess.run([find_pg_bin("pg_dump"), "--version"],
                         capture_output=True, text=True, encoding="utf-8").stdout.strip()
    meta = {
        "dump": dump.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": masked(cfg),
        "pg_dump": ver,
        "tables": before["counts"],
        "fingerprints": before["fingerprints"],
    }
    (out_dir / f"{dump.name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return meta


def prune(out_dir: Path = OUT_DIR, keep: int = 10) -> list[str]:
    """只保留最近 keep 份（dump + meta 成对删），返回被删的名单。"""
    dumps = sorted(out_dir.glob("*.dump"), key=lambda x: x.stat().st_mtime, reverse=True)
    gone = []
    for d in dumps[keep:]:
        m = out_dir / f"{d.name}.meta.json"
        for f in (d, m):
            if f.exists():
                f.unlink()
                gone.append(f.name)
    return gone


def main(argv: list[str]) -> int:
    dsn, out, keep = CLIENT_DSN, OUT_DIR, 10
    for a in argv:
        if a.startswith("--dsn="):
            dsn = a.split("=", 1)[1]
        elif a.startswith("--out="):
            out = Path(a.split("=", 1)[1])
            out = out if out.is_absolute() else (ROOT / out)
        elif a.startswith("--keep="):
            keep = int(a.split("=", 1)[1])
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            return 2
    try:
        meta = backup(dsn, out)
        gone = prune(out, keep) if keep > 0 else []
    except Exception as e:  # noqa: BLE001
        print(f"[自身错误] {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(f"[ok] {meta['dump']}  {meta['bytes']} 字节  sha256={meta['sha256'][:16]}…")
    print(f"     {meta['pg_dump']}  源 {meta['source']['db']}@{meta['source']['host']}:{meta['source']['port']}")
    print("     行数 " + " ".join(f"{k}={v}" for k, v in meta["tables"].items()))
    print("     指纹 " + " ".join(f"{k}={v}" for k, v in meta["fingerprints"].items()))
    if gone:
        print(f"[prune] 删掉较旧 {len(gone)} 个文件（keep={keep}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
