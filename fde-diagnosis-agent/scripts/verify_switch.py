# -*- coding: utf-8 -*-
"""换源验收（T10）：八条判据指到第二家客户 fde_client，逐个反例看它落在哪一档。

做法：反例都造在 `probe` schema 里——`create table ... (like public.md_device)`
拿结构，再改一处；登记用的临时副本写在 data/mapping/source_schema，跑完删掉。
客户真表一行不动，所以"拒跑"证明的是判据，不是我们手快。

T10 时判据逻辑一条没改；唯一一次改判据是 S5 的量纲（T12）：从「每个值都小」改成
「≥90% 的值支持缩放假设」。理由与双向反例见 verify 里的 e8（带脏值仍须拒跑）
与 e9（只有脏值、量纲未变不许拒跑）——这两个是成对的，少一个就把另一个的自由度放开了。

用法（在 fde-diagnosis-agent 目录）：
    python -X utf8 scripts/verify_switch.py
前置：fde_client 可连（postgres/postgres），八条判据在基线已跑通。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REG_DIR = ROOT / "data" / "mapping" / "source_schema"
PY = sys.executable

DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
SCHEMA = "probe"

# 每个反例：造法（SQL）、期望 exit、期望出现在输出里的关键词
CASES = [
    ("e1_新增列", "ALTER TABLE probe.e1 ADD COLUMN torque_limit numeric",
     1, ["S1 新增列", "torque_limit"]),
    ("e2_删非主键列", "ALTER TABLE probe.e2 DROP COLUMN lens_spec",
     1, ["S1 消失列", "lens_spec"]),
    ("e3_主键列消失", "ALTER TABLE probe.e3 DROP COLUMN equip_no",
     3, ["S3 拒跑", "equip_no"]),
    ("e4_新增必填列", "ALTER TABLE probe.e4 ADD COLUMN batch_no text NOT NULL DEFAULT 'B-0001'",
     3, ["S3 拒跑", "batch_no"]),
    # 未声明改名在非空列上会被 S3 兜住：新名对历史行而言就是"新增必填列"。
    # 所以这条期望 3 而不是 1——两条规则叠加时取严格的那个。
    ("e5_未声明改名(非空列→S3兜住)", "ALTER TABLE probe.e5 RENAME COLUMN lens_spec TO lens_model",
     3, ["S4 疑似改名", "S3 拒跑", "lens_model"]),
    ("e5b_未声明改名(可空列→S4单独红)", "ALTER TABLE probe.e5b RENAME COLUMN tmp_note TO tmp_doc",
     1, ["S4 疑似改名", "tmp_doc"]),
    ("e6_未声明改名(主键)", "ALTER TABLE probe.e6 RENAME COLUMN equip_no TO device_no",
     3, ["S3 拒跑", "疑似改名的正是主键列"]),
    ("e7_类型变", "ALTER TABLE probe.e7 ALTER COLUMN maint_cycle TYPE text",
     1, ["S1 类型变", "smallint -> text"]),
]


def run(args: list[str], env_extra: dict | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([PY, "-X", "utf8"] + args, cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def psql(sql: str) -> tuple[int, str]:
    """一段 SQL 通过临时文件喂给 psql —— 命令行里带中文会撞编码，落文件最稳。"""
    import psycopg
    try:
        with psycopg.connect(DSN) as conn:
            conn.execute(sql)
        return 0, ""
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def probe_registry(case_key: str, table: str, extra_col: dict | None = None) -> Path:
    """把 md_device 的登记抄一份，指向 probe.<table>。登记说什么，判据就比什么。"""
    src = json.loads((REG_DIR / "client_mes_md_device.json").read_text(encoding="utf-8"))
    payload = dict(src)
    if extra_col:
        payload["columns"] = list(payload["columns"]) + [extra_col]
    payload["client"] = "client_probe"
    payload["table"] = case_key
    payload["source_path"] = "postgresql://localhost:5432/fde_client (%s.%s)" % (SCHEMA, table)
    payload["observed_at"] = "2026-11-26"
    p = REG_DIR / f"client_probe_{case_key}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def probe_csv(path: Path, divide: bool) -> int:
    """缺陷记录前 20 行做成 CSV 探针：列名、类型、列序一个字没变。

    divide=True  —— 把 conf_pct 除以 100，模拟"百分制改小数"；
    divide=False —— 量纲一个字不动，只保留库里原有的 150/-10 两行脏值。
    两件事合起来才是完整的证据：带脏值时**该拒还得拒**（否则一个 150 就能掩盖真实跳变），
    没改量纲时**不许拒**（否则脏值被当成量纲证据，每次全量同步都误拦）。
    时间一律按声明 format 归一，否则会先撞「日期格式改了」这个 breaking，把量纲的结论盖住。
    """
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        rows = conn.execute(
            'select rec_id, captured_at, equip_no, work_station, defect_name, conf_pct,'
            ' lot_no, judge, is_ok from public.biz_defect_record'
            ' where rec_id >= 1 order by rec_id limit 20'
        ).fetchall()
    cols = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            r = dict(r)
            if divide and r.get("conf_pct") is not None:
                r["conf_pct"] = round(float(r["conf_pct"]) / 100.0, 3)
            if r.get("captured_at") is not None:
                r["captured_at"] = r["captured_at"].strftime("%Y-%m-%d %H:%M")
            w.writerow(r)
    return len(rows)


def cleanup() -> None:
    psql("DROP SCHEMA IF EXISTS probe CASCADE")
    for p in REG_DIR.glob("client_probe_*.json"):
        p.unlink()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="跑完不清理 probe schema 与临时登记（排查用）")
    args = ap.parse_args(argv)

    ok = True
    results = []

    def chk(name: str, got, want) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(("[ok] " if good else "[!!] ") + name + " got=%r want=%r" % (got, want))

    print("== 基线（干净）")
    base = [
        ("A/B/C", ["scripts/check_status_policy.py"], 0, []),
        ("S1–S4", ["scripts/check_source_schema.py", "--client=client_mes", "--dsn=" + DSN], 0,
         ["S2 未消费"]),
        ("S5", ["scripts/check_source_values.py", "--client=client_mes", "--dsn=" + DSN], 0,
         ["S5 黄 conf_pct"]),
    ]
    for name, cmd, want, tokens in base:
        code, out = run(cmd)
        chk(name + " exit", code, want)
        for t in tokens:
            chk(name + " 含「%s」" % t, t in out, True)
        print("  " + "\n  ".join(l for l in out.strip().splitlines() if l.strip())[:400])

    print("== 反例（probe schema，客户表不动）")
    psql("DROP SCHEMA IF EXISTS probe CASCADE")
    psql("CREATE SCHEMA probe")
    for i, (name, ddl, want_code, tokens) in enumerate(CASES, 1):
        tbl = name.split("_")[0]
        psql("CREATE TABLE probe.%s (LIKE public.md_device)" % tbl)
        if tbl == "e5b":
            psql("ALTER TABLE probe.e5b ADD COLUMN tmp_note text")
        code, err = psql(ddl)
        if code != 0:
            chk(name + " 造景失败", err, "")
            continue
        extra = {"name": "tmp_note", "type": "text", "required": False} if tbl == "e5b" else None
        reg = probe_registry(tbl, tbl, extra)
        code, out = run(["scripts/check_source_schema.py", "--client=client_probe",
                         "--table=" + tbl, "--dsn=" + DSN])
        chk("%s exit" % name, code, want_code)
        for t in tokens:
            chk("%s 含「%s」" % (name, t), t in out, True)
        results.append((name, want_code, code))
        print("  " + "\n  ".join(l.strip() for l in out.strip().splitlines())[:300])
        reg.unlink(missing_ok=True)

    print("== S5 量纲跳变（CSV 探针，列名/类型不变）")
    tmp = Path(tempfile.gettempdir()) / "probe_conf_scaled.csv"
    n = probe_csv(tmp, divide=True)
    print("  探针 %s：%d 行，conf_pct 已 ÷100（库里 150/-10 两行脏值原样带入）" % (tmp, n))
    code, out = run(["scripts/check_source_values.py", "--client=client_mes",
                     "--table=biz_defect_record", "--source=" + str(tmp), "--limit=20"])
    chk("e8_量纲缩小100倍(带脏值) exit", code, 3)
    chk("e8 含「量纲疑似缩小 100 倍」", "量纲疑似缩小 100 倍" in out, True)
    chk("e8 含「中位数」", "中位数" in out, True)
    results.append(("e8_量纲缩小(带脏值)", 3, code))
    print("  " + "\n  ".join(l.strip() for l in out.strip().splitlines())[:300])

    print("== S5 不误报：量纲没变，只有脏值")
    tmp2 = Path(tempfile.gettempdir()) / "probe_conf_unscaled.csv"
    n2 = probe_csv(tmp2, divide=False)
    print("  探针 %s：%d 行，conf_pct 一个数没改（只带 150/-10 两行脏值）" % (tmp2, n2))
    code, out = run(["scripts/check_source_values.py", "--client=client_mes",
                     "--table=biz_defect_record", "--source=" + str(tmp2), "--limit=20"])
    chk("e9_脏值但量纲未变 exit", code, 0)
    chk("e9 不含「量纲疑似」", "量纲疑似" in out, False)
    chk("e9 仍报「超出声明」", "超出声明" in out, True)
    results.append(("e9_脏值但量纲未变", 0, code))
    print("  " + "\n  ".join(l.strip() for l in out.strip().splitlines())[:300])

    print("== 收尾")
    if not args.keep:
        cleanup()
        chk("probe schema 已清", psql("select 1")[0], 0)
        chk("临时登记已清", len(list(REG_DIR.glob("client_probe_*.json"))), 0)
    code, out = run(["scripts/check_source_schema.py", "--client=client_mes", "--dsn=" + DSN])
    chk("清完基线仍 exit 0", code, 0)
    code, out = run(["scripts/check_source_schema.py"])
    chk("旧客户 client_a 仍 exit 0", code, 0)

    print("\n档位汇总：")
    for name, want, got in results:
        print("  %-24s 期望 exit=%d 实得 exit=%d %s" % (name, want, got, "" if want == got else "  <-- 偏差"))
    print("RESULT:", "ALL PASS" if ok else "HAS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
