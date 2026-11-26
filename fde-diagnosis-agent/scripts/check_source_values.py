"""源端值域抽查（S5）：列名类型都对，值的意思变了。

S1 只看得到「列还在不在」。量纲从百分制改成小数、日期格式换一版、枚举多一个值、
必填列开始给空——这些一个字段都没改，S1 全绿，但结论会静默全错。

三档：
  exit 3  量纲跳变 / 日期格式整体改 / 全部不可解析 —— 基准变了，拒跑
  exit 1  超范围 / 部分不可解析 —— 可能是新边界，先看，人来决定
  exit 0  枚举扩值 / 空值率 / 声明允许的空 —— 清单，理由写进登记

用法：
    python scripts/check_source_values.py
    python scripts/check_source_values.py --table=defect_logs --source=<另一份 CSV> --limit=50
    python scripts/check_source_values.py --json=eval/source_values.json
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ds import source_schema as ss   # noqa: E402
from app.ds import value_domain as vd    # noqa: E402


def main(argv: list) -> int:
    client, only_table, source, json_out = "client_a", "", "", ""
    limit = vd.DEFAULT_LIMIT
    for a in argv:
        if a.startswith("--client="):
            client = a.split("=", 1)[1]
        elif a.startswith("--table="):
            only_table = a.split("=", 1)[1]
        elif a.startswith("--source="):
            source = a.split("=", 1)[1]
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        elif a.startswith("--json="):
            json_out = a.split("=", 1)[1]

    reg_dir = ss.REGISTRY_DIR
    tables = sorted(p.stem[len(client) + 1:] for p in reg_dir.glob(f"{client}_*.json"))
    if only_table:
        tables = [only_table]

    results, worst = [], 0
    for t in tables:
        r = vd.analyze(client, t, source or None, limit)
        results.append(r)
        worst = max(worst, r["exit"])
        tag = {3: "FAIL-拒跑", 1: "FAIL", 0: "ok" if not r["findings"] else "warn"}[r["exit"]]
        print(f"[{tag}] {client}/{t}  rows={r['rows_scanned']}  列={r['domains_checked']}")
        for f in r["findings"]:
            mark = {"breaking": "S5 拒跑", "red": "S5", "yellow": "S5 黄", "note": "S5 记"}[f["severity"]]
            print(f"        {mark} {f['column']}：{f['message']}"
                  + (f"  样本 {f['samples']}" if f["samples"] else ""))
        if r["rows_scanned"] >= limit:
            print(f"        注意：样本只抽了前 {limit} 行（--limit 可调），不是全表结论")

    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"client": client, "tables": results}, ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        print(f"json -> {out}")
    return worst


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        print("判据脚本自身崩了（这一轮结果不可当结论）：", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(2)