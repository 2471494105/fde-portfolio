#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C1 还原：把 dump 还原到**校核库**，顺手核对行数/指纹跟备份那一刻对不对。

为什么不给「还原到客户库」的入口
------------------------------
`fde_client` 是客户库。一个能一条命令覆盖客户库的脚本，事故率远高于它节省的时间：
回车按错一次、参数写反一次，就是客户数据没了。所以这里做两道硬拦：

1. `--target` 必须以 `fde_client_restore` 开头，默认 `fde_client_restore_check`；
2. `fde_client`（及任何非校核库名）直接拒绝，**没有** `--force` 之类的旁路。

真要恢复生产，走「建新库 → 还原 → 人工切换连接串」，脚本只负责前半段。

用法：
    python -X utf8 scripts/restore_db.py --dump=out/backup/fde_client_20261127_133903.dump --yes
    python -X utf8 scripts/restore_db.py --dump=... --target=fde_client_restore_check --yes --keep

退出码：0 还原且核对一致 / 1 还原了但核对不一致 / 2 自身出错
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backup_db import CLIENT_DSN, env_for, find_pg_bin, parse_dsn, snapshot  # noqa: E402

GUARD_PREFIX = "fde_client_restore"
ADMIN_DB = "postgres"


def dsn_with_db(dsn: str, db: str) -> str:
    u = urlsplit(dsn)
    return f"postgresql://{u.username}:{u.password}@{u.hostname}:{u.port or 5432}/{db}"


def reset_db(src_dsn: str, target: str) -> None:
    """重建目标库：存在就 FORCE 掉（PG13+），不存在就建。"""
    import psycopg

    cfg = parse_dsn(src_dsn)
    with psycopg.connect(dsn_with_db(src_dsn, ADMIN_DB), connect_timeout=10,
                         autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (target,))
            if cur.fetchone():
                cur.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{target}" TEMPLATE template0')
    del cfg


def restore(dump: Path, src_dsn: str, target: str, keep: bool = False) -> dict:
    if not dump.is_file():
        raise RuntimeError(f"dump 不存在：{dump}")
    head = dump.read_bytes()[:5]
    if head != b"PGDMP":
        raise RuntimeError(f"不是 pg_dump 自定义格式的文件头：{head!r}（别拿 .sql 冒充）")

    cfg = parse_dsn(src_dsn)
    reset_db(src_dsn, target)
    try:
        p = subprocess.run(
            [find_pg_bin("pg_restore"), "-h", cfg["host"], "-p", cfg["port"], "-U", cfg["user"],
             "-d", target, "--no-owner", "--exit-on-error", str(dump)],
            env=env_for(cfg), capture_output=True, text=True, encoding="utf-8",
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"pg_restore 失败 exit={p.returncode}（半个库不算还原）："
                f"{(p.stderr or p.stdout)[-500:]}")
    except Exception:
        # 失败也要收尾：否则校核库留在半还原状态，下一次 reset 会变慢且容易误判
        if not keep:
            try:
                with_psycopg_drop(src_dsn, target)
            except Exception:  # noqa: BLE001
                pass
        raise

    after = snapshot(dsn_with_db(src_dsn, target))
    result = {"target": target, "restored": after, "dump": dump.name}
    if not keep:
        # 校核完就拆掉，避免下次 `pg_database` 里多一个同名库把 reset 变慢
        with_psycopg_drop(src_dsn, target)
        result["dropped"] = True
    return result


def with_psycopg_drop(src_dsn: str, target: str) -> None:
    import psycopg

    with psycopg.connect(dsn_with_db(src_dsn, ADMIN_DB), connect_timeout=10,
                         autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')


def compare(meta: dict | None, restored: dict) -> tuple[bool, list[str]]:
    """有 meta 时逐表核对行数与指纹；没 meta 只算「还原成功」。"""
    if not meta:
        return True, ["无 meta，只验「还原没报错」"]
    diffs = []
    for t, n in meta["tables"].items():
        got = restored["counts"].get(t)
        if got != n:
            diffs.append(f"行数 {t}: 备份 {n} → 还原 {got}")
    for t, fp in meta["fingerprints"].items():
        got = restored["fingerprints"].get(t)
        if got != fp:
            diffs.append(f"指纹 {t}: 备份 {fp} → 还原 {got}")
    return (not diffs), (diffs or ["六表行数与两表指纹一致"])


def main(argv: list[str]) -> int:
    dump, target, src, yes, keep = None, f"{GUARD_PREFIX}_check", CLIENT_DSN, False, False
    for a in argv:
        if a.startswith("--dump="):
            dump = Path(a.split("=", 1)[1])
            dump = dump if dump.is_absolute() else (ROOT / dump)
        elif a.startswith("--target="):
            target = a.split("=", 1)[1]
        elif a.startswith("--dsn="):
            src = a.split("=", 1)[1]
        elif a == "--yes":
            yes = True
        elif a == "--keep":
            keep = True
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            return 2

    if dump is None:
        print("[用法] --dump=<备份文件> --yes [--target=fde_client_restore_xxx] [--keep]", file=sys.stderr)
        return 2
    if not target.startswith(GUARD_PREFIX):
        print(f"[拒绝] 目标库「{target}」不是校核库：只允许 {GUARD_PREFIX}* 开头。"
              f"恢复生产走「建新库→还原→人工切连接串」，本脚本不覆盖客户库。", file=sys.stderr)
        return 2
    if not yes:
        print("[拒绝] 还原会 DROP 并重建目标库，必须显式 --yes。", file=sys.stderr)
        return 2

    meta_path = dump.with_name(dump.name + ".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else None
    try:
        result = restore(dump, src, target, keep)
    except Exception as e:  # noqa: BLE001
        print(f"[自身错误] {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    ok, notes = compare(meta, result["restored"])
    print(f"[{'ok' if ok else '不一致'}] 还原到 {target}（源 dump {result['dump']}）")
    for n in notes:
        print(f"     {n}")
    print("     行数 " + " ".join(f"{k}={v}" for k, v in result["restored"]["counts"].items()))
    print("     指纹 " + " ".join(f"{k}={v}" for k, v in result["restored"]["fingerprints"].items()))
    if result.get("dropped"):
        print(f"[clean] 校核库 {target} 已拆掉（--keep 可保留）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
