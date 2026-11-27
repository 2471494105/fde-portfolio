"""验 C1 备份/还原：文件能出、meta 不落密码、还原对得上、坏文件与护栏都拦得住。

V1 备份能出：dump 文件头是 PGDMP、字节不为 0、meta 的 sha256 与文件实算一致
V2 meta 不落密码：meta 里只有 host/port/db，出现 password 或明文口令即失败
V3 还原对得上：还原到校核库后六表行数与两张表指纹等于备份那一刻
V4 坏文件拦得住：截断的 dump 必须 exit 2，不许「还原了个空库还报成功」
V5 护栏：`--target=fde_client` 拒绝（exit 2）；不给 `--yes` 拒绝（exit 2）
V6 校核库不残留：还原完默认 DROP，`pg_database` 里查不到
V7 轮转：keep=1 时临时目录里只剩最近一份（dump 与 meta 成对删）
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
BACKUP = ROOT / "scripts" / "backup_db.py"
RESTORE = ROOT / "scripts" / "restore_db.py"
DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
TARGET = "fde_client_restore_check"


def run(script: Path, args: list[str], timeout: int = 180) -> tuple[int, str]:
    p = subprocess.run([PY, "-X", "utf8", str(script), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def meta_of(dump: Path) -> dict:
    return json.loads(Path(str(dump) + ".meta.json").read_text(encoding="utf-8"))


def db_exists(name: str) -> bool:
    import psycopg

    with psycopg.connect(DSN.replace("/fde_client", "/postgres"), autocommit=True) as c:
        return bool(c.execute("select 1 from pg_database where datname=%s", (name,)).fetchone())


def main(argv: list[str]) -> int:
    fails: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('：' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # V1 备份能出
        code, out = run(BACKUP, [f"--out={tmp}"])
        dumps = sorted(tmp.glob("*.dump"))
        if not dumps:
            check(False, "V1a 备份能出文件", out[-300:])
            print()
            print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
            return 1
        dump = dumps[-1]
        meta = meta_of(dump)
        head = dump.read_bytes()[:5]
        real = hashlib.sha256(dump.read_bytes()).hexdigest()
        check(code == 0 and head == b"PGDMP" and dump.stat().st_size > 0,
              "V1a 备份能出：exit 0、文件头 PGDMP、非空",
              f"exit={code} head={head!r} bytes={dump.stat().st_size}")
        check(meta["sha256"] == real, "V1b meta 的 sha256 与文件实算一致",
              f"{meta['sha256'][:16]}…")
        check(set(meta["tables"]) == {"biz_work_order", "biz_defect_record", "biz_alarm",
                                      "biz_repair_order", "md_station", "md_device"}
              and meta["fingerprints"]["biz_work_order"] == "df7c564b4185077b",
              "V1c meta 里有六表行数，工单指纹等于演示基线",
              f"wo={meta['fingerprints']['biz_work_order']}")

        # V2 meta 不落密码
        raw = Path(str(dump) + ".meta.json").read_text(encoding="utf-8").lower()
        check("password" not in raw and "postgres:postgres" not in raw,
              "V2 meta 不落密码（只有 host/port/db）",
              f"source={meta['source']}")
        check("db" in meta["source"] and "user" not in meta["source"],
              "V2b meta.source 不含用户名", str(meta["source"]))

        # V3 还原对得上
        code_r, out_r = run(RESTORE, [f"--dump={dump}", "--yes"])
        restored_ok = code_r == 0 and "六表行数与两表指纹一致" in out_r
        check(restored_ok, "V3 还原到校核库后六表行数与两表指纹一致", f"exit={code_r}")

        # V6 校核库不残留
        check(not db_exists(TARGET), "V6 还原完默认拆掉校核库（不残留）")

        # V4 坏文件拦得住
        bad = tmp / "truncated.dump"
        bad.write_bytes(dump.read_bytes()[:64])
        code_b, out_b = run(RESTORE, [f"--dump={bad}", "--yes"])
        check(code_b == 2 and ("pg_restore 失败" in out_b or "文件头" in out_b),
              "V4 截断的 dump 拒绝（exit 2，不当成功）", f"exit={code_b}")
        check(not db_exists(TARGET), "V4c 还原失败也收尾，校核库不留半吊子")
        sql_like = tmp / "fake.sql"
        sql_like.write_text("-- not a dump\n", encoding="utf-8")
        code_s, _ = run(RESTORE, [f"--dump={sql_like}", "--yes"])
        check(code_s == 2, "V4b 拿 .sql 冒充 dump 也拒绝", f"exit={code_s}")

        # V5 护栏
        code_t, out_t = run(RESTORE, [f"--dump={dump}", "--yes", "--target=fde_client"])
        check(code_t == 2 and "不是校核库" in out_t,
              "V5a 拒绝还原到客户库（无旁路）", f"exit={code_t}")
        code_n, out_n = run(RESTORE, [f"--dump={dump}"])
        check(code_n == 2 and "--yes" in out_n, "V5b 不给 --yes 拒绝", f"exit={code_n}")

        # V7 轮转
        code_p, out_p = run(BACKUP, [f"--out={tmp}", "--keep=1"])
        left = sorted(tmp.glob("*.dump"))
        metas = sorted(tmp.glob("*.dump.meta.json"))
        check(code_p == 0 and len(left) == 1 and len(metas) == 1,
              "V7 keep=1 轮转：只剩最近一份，meta 成对删",
              f"dumps={[x.name for x in left]} metas={[x.name for x in metas]}")

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项没过：{'、'.join(fails)}")
        return 1
    print("[OK] C1 备份/还原全部通过（能出、不带密码、还原对得上、坏文件与护栏都拦、轮转不残留）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(2)
