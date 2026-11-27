#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一条命令导出「待处置清单」：三条活事件 → 一份 CSV（只读，不写客户库）。

为什么要有这一步
----------------
判据答的是「有多少行不对」，明细页答的是「是哪几行、长什么样」。客户下一句是
「行，那谁去做」——没有清单，前面的答案就停在屏幕上，带不进他们的排班表。

三条口径
1. **只导出，不代填**：CSV 只写「要改哪一栏、改成什么规则」，不写回 `acked_by` /
   `cleared_at`。关闭仍只认判据复跑归零，导出一张表不代表销账。
2. **数字来自接口，不重算**：行数取 `/v1/problem/{id}` 的 `ids_total`，这里不查库、
   不数第二遍；页面上写 40，导出就该是 40 行。
3. **空值照原样**：原值 JSON 里 `null` 就是库里空着，不补 0、不补空串。
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out" / "actions"
EXPORT_COLS = 14  # 事件编号…判据跑的时间，改列数要同步改 README


def _get(path: str, port: int, raw: bool = False):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=30) as r:
        body = r.read()
    if raw:
        return body
    return json.loads(body.decode("utf-8"))


def usage() -> str:
    return ("用法：export_actions.py [--port=8021] [--out=<csv>] [--alert=1,2,9] [--quiet]\n"
            "  默认：取 /v1/alerts 里所有 state=open 的事件，合并成一份 CSV 落到 out/actions/\n"
            "  退出码：0 导出成功（含「今天没有活事件」）/ 2 参数错或接口不通")


def main(argv: list[str]) -> int:
    port, out, quiet = 8021, None, False
    alerts_arg = None
    for a in argv:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        elif a.startswith("--out="):
            out = Path(a.split("=", 1)[1])
        elif a.startswith("--alert="):
            alerts_arg = [int(x) for x in a.split("=", 1)[1].split(",") if x.strip()]
        elif a == "--quiet":
            quiet = True
        else:
            print(f"未知参数：{a}\n{usage()}", file=sys.stderr)
            return 2

    try:
        if alerts_arg:
            ids = alerts_arg
        else:
            rows = _get("/v1/alerts?size=500&view=full", port)["items"]
            ids = [r["alert_id"] for r in rows if r.get("state") == "open"]
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"接口不通（8021 起了吗？）：{e}", file=sys.stderr)
        return 2

    header, body, per = None, [], []
    for aid in ids:
        try:
            csv_bytes = _get(f"/v1/problem/{aid}/export.csv", port, raw=True)
        except urllib.error.HTTPError as e:
            print(f"[跳过] 事件 #{aid}：HTTP {e.code}", file=sys.stderr)
            continue
        reader = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        if not reader:
            continue
        if header is None:
            header = reader[0]
        body.extend(reader[1:])
        per.append((aid, len(reader) - 1))

    if header is None:
        print("今天没有活事件，没有清单可导（这不等于数据没问题——判据绿才算）。")
        return 0

    out = out or (OUT_DIR / f"待处置清单_{datetime.now():%Y%m%d_%H%M}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(body)
    out.write_bytes(("\ufeff" + buf.getvalue()).encode("utf-8"))

    if not quiet:
        for aid, n in per:
            print(f"  事件 #{aid} → {n} 行")
        print(f"合计 {len(body)} 行；清单：{out}")
        print("只导出不代填：认领与销账仍只认同一道判据复跑归零。")
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
