"""数据健康检查的一键入口（G6）。

为什么要这一层：契约断言 / 跨源矩阵 / 增量水位三块证据各自有脚本，但评测报告
需要的是**一份能写进 meta 的汇总**。让 run_eval 现场跑它们不行——它们要连库、
会 TRUNCATE 沙箱表、耗时不可控，而评测本身已经 53 分钟；更糟的是「跑模型」和
「查数据」两个动作的失败会混在一起，定位时得先猜是谁坏的。

所以分工成这样：

    本脚本   = 动作。跑三块检查，汇总落盘 eval/data_health.json
    run_eval = 只读。把这份结果写进报告 meta，标上时效（多少分钟前的检查）

顺序有讲究：矩阵和增量都会动沙箱 staging 表，契约检查跑主库 fde_rag 不受影响；
反过来先跑契约再跑增量也没事，但矩阵最后会把 stg 清空，所以增量放中间、
契约放最后，跑完沙箱里留下的是增量那套 14 行数据，便于人工核对。

用法：
    python scripts/data_health.py
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_data_contract as cdc  # noqa: E402
import check_ds_incremental as incr  # noqa: E402
import check_ds_matrix as matrix  # noqa: E402

PROD_DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"
SBX_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"
OUT = ROOT / "eval" / "data_health.json"


def _run(func, argv, path):
    """跑一个检查并读回它的 JSON。拿不到 JSON 记 error，不冒充 ok。

    退出码不参与判断：非零只说明「检查发现了问题」，那件事已经写在 JSON 的
    status 里；两个信号并存会让人在两者不一致时不知道信哪个。
    """
    func(argv)
    if not path.exists():
        return {"status": "error", "note": "检查没落盘（脚本可能提前退出）"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "note": f"JSON 读不出来：{type(e).__name__}: {e}"}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="data_health_"))

    print("== [1/3] 跨源矩阵：四种形态同一套断言 ==")
    m = _run(matrix.main, [f"--json={tmp / 'matrix.json'}"], tmp / "matrix.json")

    print("\n== [2/3] 增量水位：八步装载 ==")
    i = _run(incr.main, [f"--json={tmp / 'incr.json'}"], tmp / "incr.json")

    print("\n== [3/3] 数据契约：主库 fde_rag（干净数据，应全绿）==")
    c = _run(
        cdc.main,
        [f"--dsn={PROD_DSN}", "--expect=clean", f"--json={tmp / 'contract.json'}"],
        tmp / "contract.json",
    )

    # 隔离区单独查沙箱：契约检查跑的是主库，主库没有 stg_quarantine。
    q = cdc.quarantine_report(SBX_DSN)

    issues = []
    if c.get("status") == "error":
        issues.append("契约检查没跑起来")
    elif c.get("status") != "ok":
        bad = c.get("fail") or []
        issues.append(f"契约断言红 {len(bad)} 条：{', '.join(bad)}")
    if m.get("status") == "error":
        issues.append("跨源矩阵没跑起来")
    elif m.get("status") != "ok":
        issues.append("四种形态的 B 类结果不一致（搬运过程中丢了信息）")
    if i.get("status") == "error":
        issues.append("增量检查没跑起来")
    elif i.get("status") != "ok":
        issues.append("增量装载出现重复 id（落库幂等失效）")

    payload = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "contract": c,
        "matrix": m,
        "incremental": i,
        "quarantine": {
            "db": "fde_sandbox",
            "rows": sum(x["n"] for x in q) if q else 0,
            "reasons": {x["reason"]: x["n"] for x in q} if q else {},
            # 这 1 条不是「生产库脏了」，是增量检查的固定夹具（2027 年那条）。
            # 不写清来源，读报告的人会以为系统在往隔离区里丢数据。
            "note": ("这条来自增量检查的固定夹具（id=4、ts=2027-01-01），是隔离功能生效的证据，"
                     "不是生产脏数据；生产库的隔离区请单独跑 check_data_contract.py --dsn=<生产库>"),
        },
        "issues": issues,
        "status": "ok" if not issues else "fail",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"数据健康      {payload['status'].upper()}")
    print(f"契约断言      {(c.get('pass'), c.get('total'))}（库 {(c.get('db') or '?')}）")
    forms = m.get("forms") or []
    print(f"跨源矩阵      {len(forms)} 种形态，B 类一致：{m.get('b_consistent')}")
    print(f"增量水位      最终 {i.get('final_rows')} 行 / {i.get('unique_ids')} 个不同 id，"
          f"重复：{i.get('duplicated')}")
    print(f"隔离区        {(payload['quarantine'])['rows']} 条（来自检查夹具，见 note）")
    if issues:
        print("\n问题：")
        for x in issues:
            print(f"  - {x}")
    else:
        print("\n没有发现问题")
    print(f"汇总已写入 {OUT.relative_to(ROOT)}")
    print("跑评测之前执行这一步，报告 meta 里才会有这四块证据")
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
