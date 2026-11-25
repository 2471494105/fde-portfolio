"""对账脚本的反向自测：故意弄坏一条，看它报不报。

一个永远绿的检查等于没有检查。这个脚本用于证明 check_ds_parity.py
抓得住差异，而不是永远打印 [OK]。

流程三步：
  1. 起点跑一次，必须 0 处不一致（否则先修数据，别自测）；
  2. 注入一条错误（CX-103 的 lens_model 改成 12mm），必须被抓到且退出码 1；
  3. 恢复，再跑一次必须回到 0 处不一致。

finally 保证 CSV 一定写回原样——自测不能把被检对象弄坏。
"""

import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "external" / "devices.csv"
PARITY = ROOT / "scripts" / "check_ds_parity.py"
TARGET_KEY = "CX-103"
TARGET_COL = "lens_model"
WRONG_VALUE = "12mm"


def load():
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), [dict(r) for r in reader]


def dump(fields, rows):
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_parity():
    proc = subprocess.run(
        [sys.executable, str(PARITY)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    fields, rows = load()
    original = [dict(r) for r in rows]

    code, out = run_parity()
    if code != 0:
        print("[1/3] 起点不绿，先修数据再自测")
        print(out)
        return 2
    print("[1/3] 起点：0 处不一致，OK")

    try:
        for rec in rows:
            if rec.get("device_id") == TARGET_KEY:
                rec[TARGET_COL] = WRONG_VALUE
        dump(fields, rows)

        code, out = run_parity()
        if code == 1 and TARGET_COL in out:
            print(f"[2/3] 注入 {TARGET_KEY}.{TARGET_COL}={WRONG_VALUE} → 抓到差异，OK")
        else:
            print(f"[2/3] 改坏后没被抓到（exit={code}）")
            print(out)
            return 1
    finally:
        dump(fields, original)

    code, out = run_parity()
    if code != 0:
        print("[3/3] 恢复后仍不绿")
        print(out)
        return 1
    print("[3/3] 恢复后：0 处不一致，OK")
    print("\n[OK] 对账抓得住差异，且能回到绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
