"""把「没有样本」的两道判据当场演红一次——不造客户数据。

为什么要这个脚本
----------------
`source_schema`（源端结构漂移）和 `status_policy` 的 B 项（库里出现未登记状态），
今天都没样本：客户源端没改过表头，库里也没有未登记的词。客户会问一句很硬的话：
「**它红过吗？**」——答「今天没有这种数据」听起来像「我不知道它会不会红」。

这个脚本用两个**临时观测面**回答：
  1. 结构：复制一份表头（改一列名 + 加一列），走 `check_source_schema.py --source=`，
     脚本本来就是为这种探针写的（不动原文件）。
  2. 口径：在 `fde_sandbox` 建一张临时表 `probe_wo_bad`，插 3 行未登记状态，
     只调 B 项那一个函数——不跑 A/C/D/E/F/G/H，避免把「演示」扩成「整套重跑」。

两条硬规矩
----------
- **不碰客户库**：两处临时对象一处在临时文件、一处只在 sandbox 里；跑完必删。
- **反例不红就是失败**：判据没红说明判据失效，脚本 exit 1，不许说「演示成功」。

用法：
    python -X utf8 scripts/probe_red.py                 # 两个都演
    python -X utf8 scripts/probe_red.py --case=source_schema
    python -X utf8 scripts/probe_red.py --case=status_policy
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import source_schema as ss  # noqa: E402
from app.ds import status_policy as sp  # noqa: E402

PY = sys.executable
CLIENT_DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
SANDBOX_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"


def wo_fingerprint(dsn: str = CLIENT_DSN) -> str:
    """客户库工单整表指纹：证明「两场演练一个字都没改到客户库」。"""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(md5(string_agg(wo_no || '|' || coalesce(prod_line, '~'), ';'"
            " ORDER BY wo_no)), 'empty') FROM biz_work_order"
        )
        return cur.fetchone()[0]


def probe_source_schema(client: str = "client_a", table: str = "devices") -> dict:
    """临时 CSV：改一列名 + 加一列，判据必须红（S1 新增/消失 + S4 疑似改名）。"""
    reg = ss.load(client, table)
    src = ROOT / reg["source_path"]
    raw = src.read_text(encoding="utf-8-sig").splitlines()
    header = raw[0].split(",")
    cols = list(header)
    cols[2] = cols[2] + "_renamed"          # 少一列，探 S1 removed / S4
    cols.append("we_added_this")            # 多一列，探 S1 added
    tmp = Path(tempfile.gettempdir()) / f"probe_{table}_{client}.csv"
    tmp.write_text("\n".join([",".join(cols)] + raw[1:]) + "\n", encoding="utf-8")

    cp = subprocess.run(
        [PY, "-X", "utf8", str(ROOT / "scripts" / "check_source_schema.py"),
         f"--client={client}", f"--table={table}", f"--source={tmp}", "--no-record"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    red = cp.returncode >= 1
    out = (cp.stdout or "") + (cp.stderr or "")
    tmp.unlink(missing_ok=True)             # 还原：临时文件不留在盘上
    return {
        "case": "source_schema",
        "probe": f"临时 CSV（改 {header[2]} → {cols[2]}，加 {cols[-1]}）",
        "exit": cp.returncode,
        "red": red,
        "lines": [l.strip() for l in out.splitlines() if l.strip()][:6],
        "restored": not tmp.exists(),
    }


def probe_status_policy() -> dict:
    """sandbox 临时表：3 行未登记状态，只调 B 项；跑完 drop。"""
    policy = sp.load()
    table = "probe_wo_bad"
    import psycopg

    with psycopg.connect(SANDBOX_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(f"CREATE TABLE {table} (wo_no text, state text)")
        cur.execute(
            f"INSERT INTO {table} VALUES ('PROBE-1','待处理'),"
            " ('PROBE-2','已完成'), ('PROBE-3','完成待确认')"
        )
    try:
        spec = importlib.util.spec_from_file_location(
            "_csp", ROOT / "scripts" / "check_status_policy.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        res = mod.check_db_states(policy, SANDBOX_DSN, table)
    finally:
        with psycopg.connect(SANDBOX_DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
    with psycopg.connect(SANDBOX_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NULL", (f"public.{table}",))
        gone = cur.fetchone()[0]
    unknown = res.get("unknown_states") or []
    return {
        "case": "status_policy",
        "probe": f"sandbox 临时表 {table}（3 行，其中「完成待确认」未登记）",
        "status": res.get("status"),
        "red": res.get("status") == "fail",
        "unknown": unknown,
        "detail": res.get("detail") or [],
        "restored": bool(gone),
    }


def main(argv: list[str]) -> int:
    case = "all"
    for a in argv:
        if a.startswith("--case="):
            case = a.split("=", 1)[1]
        else:
            print(f"未知参数 {a!r}", file=sys.stderr)
            return 2
    if case not in ("all", "source_schema", "status_policy"):
        print(f"不认识的 case {case!r}（all / source_schema / status_policy）", file=sys.stderr)
        return 2

    fp0 = wo_fingerprint()
    print(f"先记客户库指纹：{fp0}（演练前后必须逐字相同）")
    results, bad = [], []
    if case in ("all", "source_schema"):
        r = probe_source_schema()
        results.append(r)
        print(f"\n[1] 结构漂移 · {r['probe']}")
        for l in r["lines"]:
            print(f"    {l}")
        print(f"    → 判据 exit={r['exit']}（红={'是' if r['red'] else '否'}）· 临时文件已删={r['restored']}")
        if not (r["red"] and r["restored"]):
            bad.append("source_schema")
    if case in ("all", "status_policy"):
        r = probe_status_policy()
        results.append(r)
        print(f"\n[2] 口径 · {r['probe']}")
        print(f"    未登记状态：{r['unknown']}")
        for d in r["detail"]:
            print(f"    {d}")
        print(f"    → B 项 status={r['status']}（红={'是' if r['red'] else '否'}）· 临时表已删={r['restored']}")
        if not (r["red"] and r["restored"]):
            bad.append("status_policy")

    fp1 = wo_fingerprint()
    same = fp0 == fp1
    print(f"\n客户库指纹：{fp1}（{'一致' if same else '变了！'}）")
    if not same:
        bad.append("客户库被改到")

    print(f"\n结论：{'两道都当场红了，且全部还原' if not bad else '有问题：' + ' / '.join(bad)}")
    print(json.dumps({"fingerprint_before": fp0, "fingerprint_after": fp1,
                      "results": results, "bad": bad}, ensure_ascii=False, indent=2))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
