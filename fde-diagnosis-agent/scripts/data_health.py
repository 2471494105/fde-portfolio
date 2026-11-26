"""数据健康检查的一键入口（G6）。

为什么要这一层：契约断言 / 跨源矩阵 / 增量水位 / 删除同步四块证据各自有脚本，但评测报告
需要的是**一份能写进 meta 的汇总**。让 run_eval 现场跑它们不行——它们要连库、
会 TRUNCATE 沙箱表、耗时不可控，而评测本身已经 53 分钟；更糟的是「跑模型」和
「查数据」两个动作的失败会混在一起，定位时得先猜是谁坏的。

所以分工成这样：

    本脚本   = 动作。跑四块检查，汇总落盘 eval/data_health.json
    run_eval = 只读。把这份结果写进报告 meta，标上时效（多少分钟前的检查）

顺序有讲究：矩阵和删除同步都会动沙箱 stg_devices，所以挨着跑；增量动 stg_defect_logs，
契约检查跑主库 fde_rag 完全不受影响。最后跑契约，沙箱状态停在删除检查那套（5 台设备、
1 台软删），便于人工核对。

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
import check_ds_delete as delsync  # noqa: E402
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

    print("== [1/4] 跨源矩阵：四种形态同一套断言 ==")
    m = _run(matrix.main, [f"--json={tmp / 'matrix.json'}"], tmp / "matrix.json")

    # 删除检查排在矩阵之后：两者都动 stg_devices，相邻跑不容易互相踩
    print("\n== [2/4] 删除同步：缺席两次才删、骤降整轮拒绝 ==")
    d = _run(delsync.main, [f"--json={tmp / 'delete.json'}"], tmp / "delete.json")

    print("\n== [3/4] 增量水位：十步装载 ==")
    i = _run(incr.main, [f"--json={tmp / 'incr.json'}"], tmp / "incr.json")

    print("\n== [4/4] 数据契约：主库 fde_rag（干净数据，应全绿）==")
    c = _run(
        cdc.main,
        [f"--dsn={PROD_DSN}", "--expect=clean", f"--json={tmp / 'contract.json'}"],
        tmp / "contract.json",
    )

    # 隔离区单独查沙箱：契约检查跑的是主库，主库没有 stg_quarantine。
    q = cdc.quarantine_report(SBX_DSN)
    q_rows = sum(x["n"] for x in q) if q else 0

    # 「隔离区有没有涨」只能跟上一次比：沙箱里那条是增量检查的固定夹具，
    # 绝对值恒为 1，一直盯着绝对值等于一直盯着一个常数。
    # 这里读 OUT 的旧值，跑完之后才会被覆盖——顺序错了就会自己跟自己比。
    #
    # 但要注意一个自指问题：增量检查每轮都会 TRUNCATE stg_quarantine 再插 1 条夹具，
    # 也就是「检查脚本重置了它自己要检查的对象」。所以在本项目里 delta 恒为 0，
    # 它测不出真实世界的变化；真实环境里隔离区不复位，这个数才是有意义的信号。
    # 判据本身验过：把旧值改成 0 再跑，delta=+1 会红；再跑一次自愈回 0。
    prev_rows = None
    if OUT.exists():
        try:
            prev_rows = (json.loads(OUT.read_text(encoding="utf-8")).get("quarantine") or {}).get("rows")
        except Exception:
            prev_rows = None
    delta = (q_rows - prev_rows) if isinstance(prev_rows, int) else None

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
    if d.get("status") == "error":
        issues.append("删除同步检查没跑起来")
    elif d.get("status") != "ok":
        issues.append("删除同步出现异常判定（误删或该删未删）")
    # 三道护栏任一失效都算红：这是「不报错但删多了」的地方，不能只看 status
    if d.get("status") == "ok":
        guards = d.get("guards") or []
        if len(guards) < 6:
            issues.append("删除同步的护栏少了（应有 6 条）")
    if q is None:
        # 表不存在和连不上库在 quarantine_report 里是同一个返回值（None），
        # 但两种都不能当成「隔离区是空的」——读不到不等于没有
        issues.append("隔离区读不到（表不存在或库连不上，不等于 0 条）")
    elif delta is not None and delta > 0:
        issues.append(f"隔离区比上次多了 {delta} 条（有新行被挡在主表外）")

    payload = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "contract": c,
        "matrix": m,
        "incremental": i,
        "delete": d,
        "quarantine": {
            "db": "fde_sandbox",
            "readable": q is not None,
            "rows": q_rows,
            # 涨没涨比有多少更重要：这个数里混着固定夹具，绝对值看不出趋势
            "prev_rows": prev_rows,
            "delta": delta,
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
    print(f"删除同步      活跃 {d.get('live_devices')} 台 / 软删 {d.get('soft_deleted')} 台 / "
          f"审计 {d.get('audit_rows')} 条 / 护栏 {len(d.get('guards') or [])} 条")
    qd = payload["quarantine"]
    delta_text = "" if qd["delta"] is None else f"（较上次 {qd['delta']:+d} 条）"
    readable_text = "" if qd["readable"] else "**读不到**："
    print(f"隔离区        {readable_text}{qd['rows']} 条{delta_text}（来自检查夹具，见 note）")
    if issues:
        print("\n问题：")
        for x in issues:
            print(f"  - {x}")
    else:
        print("\n没有发现问题")
    print(f"汇总已写入 {OUT.relative_to(ROOT)}")
    print("跑评测之前执行这一步，报告 meta 里才会有这五块证据")
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
