"""数据健康检查的一键入口（G6）。

为什么要这一层：契约断言 / 跨源矩阵 / 增量水位 / 删除同步 / 事实层五块证据各自有脚本，
但评测报告需要的是**一份能写进 meta 的汇总**。让 run_eval 现场跑它们不行——它们要连库、
会 TRUNCATE 沙箱表、耗时不可控，而评测本身已经 53 分钟；更糟的是「跑模型」和
「查数据」两个动作的失败会混在一起，定位时得先猜是谁坏的。

所以分工成这样：

    本脚本   = 动作。跑五块检查，汇总落盘 eval/data_health.json
    run_eval = 只读。把这份结果写进报告 meta，标上时效（多少分钟前的检查）

顺序有讲究：矩阵和删除同步都会动沙箱 stg_devices，所以挨着跑；增量动 stg_defect_logs，
契约检查跑主库 fde_rag 完全不受影响；事实层横跨两库（当前值读沙箱、历史写主库），
放最后——它会 TRUNCATE 沙箱再恢复，放在中间会打乱后面几块的前提。

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
import check_facts as factschk  # noqa: E402

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


def quarantine_probe_issues(qp):
    """隔离区探针证据的判据，抽成纯函数好被单测钉住。

    只看三个真查库的数字，不看增量脚本自己的 ok 字段——自评不算证据。
    """
    if not qp:
        return ["隔离区增量没被验过（增量检查没给探针证据，Δ>0 这条判据可能从未触发）"]
    before, after, restored = qp.get("before"), qp.get("after"), qp.get("restored")
    if not all(isinstance(x, int) for x in (before, after, restored)):
        return ["隔离区探针证据不完整（before/after/restored 有缺失）"]
    if after - before != 1:
        return [f"隔离区探针没造出 +1（{before} → {after}）：隔离区上涨已不可观测"]
    if restored != before:
        return [f"隔离区探针没回收干净（剩 {restored}，原 {before}）"]
    return []


def time_axis_issues(ta):
    """时间轴口径的判据：登记是否自洽、探针是否真的把「该漏的行」漏掉了。

    只看登记内容与三个数字，不看增量脚本自评的 ok。它已经在第 [13] 步自己判过一次，
    这里再判一次是刻意的重复——自评会错，规则和数字不会同时错。

    K-3：源端过滤字段、本地水位字段、业务时间字段不是同一个「钟」时，增量会静默丢。
    名字一样不算数（名字一样、语义不同的两个 ts 更危险），所以这里既核对登记，
    也要求探针证明「业务时间早于水位的行确实没被灌进来」。
    """
    if not ta:
        return ["时间轴没被验过（增量检查没给 time_axis 证据，K-3 那条口径等于没落）"]
    out = []
    reg = ta.get("registry") or {}
    if ta.get("static_issues"):
        out.append("时间轴登记自相矛盾：" + "；".join(ta["static_issues"]))
    elif "static_issues" not in ta:
        out.append("时间轴的登记核对结果缺失（只写了登记，没核对）")
    pr = ta.get("probe") or {}
    added = pr.get("rows_added")
    if not isinstance(added, int):
        out.append("时间轴探针证据不完整（rows_added 缺失）")
    elif added != 0:
        out.append(
            f"时间轴探针被灌进 {added} 条（业务时间早于水位的行本该被判已灌过）："
            "代码读的不是业务时间字段，真源上就是补录单整批进不来"
        )
    if pr.get("left_behind") is True:
        out.append("时间轴探针没清理干净（主表留下了探针行）")
    biz = (reg.get("business_time_field") or "").strip()
    plan_key = (ta.get("plan_ts_key") or "").strip()
    if not biz:
        out.append("时间轴登记缺 business_time_field")
    elif plan_key != biz:
        out.append(
            f"plan 实际用的字段（{plan_key or '空'}）与登记的业务时间字段（{biz}）不一致："
            "登记没被真的用上"
        )
    return out


def facts_issues(f):
    """事实层的判据：只看真查库的数字，不看脚本自评的 status。

    L1 只有一种失败形态——**历史看起来在，实际不可信**：
    改值没作废旧行（两条同时有效）、新行没指回旧行（链条断了）、
    同键第二条当前值没被拒（约束是装饰）、历史被沙箱重置清掉（放错了库）、
    或与当前值对不上（两套真相打架）。任何一条成立，L1 就只是「多了一张表」，
    不是「多了一个版本」。
    """
    if not f:
        return ["事实层没被验过（检查没给证据，L1 等于没落地）"]
    out = []
    v = f.get("versioning") or {}
    if v.get("old_closed") is not True:
        out.append("改值没有作废旧行（valid_to 为空：两条同时有效）")
    if v.get("new_supersedes_old") is not True:
        out.append("新行没指回旧行（supersedes 缺失：看不出是从哪个值改过来的）")
    if v.get("view_single_row") is not True:
        out.append("视图里这个键不止一行当前值")

    u = f.get("uniqueness") or {}
    if u.get("rejected") is not True:
        out.append("同键第二条当前值没被唯一索引拒绝（约束是装饰）")

    c = f.get("consistency") or {}
    if not c.get("checked"):
        out.append("事实层没做当前值一致性比对（不知道它和 stg_devices 是否对得上）")
    elif c.get("mismatched"):
        out.append(f"facts_current 与 stg_devices 有 {len(c['mismatched'])} 个键不一致"
                   "（两套真相在当前维度打架，这不是设计）")

    s = f.get("survives_sandbox_reset") or {}
    before, after = s.get("facts_before"), s.get("facts_after")
    if not (isinstance(before, int) and isinstance(after, int)):
        out.append("沙箱重置探针证据不完整（缺 before/after）")
    elif after != before:
        out.append(f"沙箱重置把历史清掉了（{before} → {after}）：facts 放错了库")
    elif s.get("sandbox_rows_after_truncate") != 0:
        out.append("沙箱 TRUNCATE 之后还有行（探针没生效，「历史独立」没被证明）")

    p = f.get("policy") or {}
    if not p.get("fact_keys"):
        out.append("事实层没带属性清单（不知道哪些列有历史）")
    if p.get("valid_from_basis") != "ingested":
        out.append(f"valid_from 口径登记成 {p.get('valid_from_basis')}："
                   "上游没有生效时间，声称 effective 就是说了一句没有证据的话")
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="data_health_"))

    print("== [1/5] 跨源矩阵：四种形态同一套断言 ==")
    m = _run(matrix.main, [f"--json={tmp / 'matrix.json'}"], tmp / "matrix.json")

    # 删除检查排在矩阵之后：两者都动 stg_devices，相邻跑不容易互相踩
    print("\n== [2/5] 删除同步：缺席两次才删、骤降整轮拒绝 ==")
    d = _run(delsync.main, [f"--json={tmp / 'delete.json'}"], tmp / "delete.json")

    print("\n== [3/5] 增量水位：十步装载 ==")
    i = _run(incr.main, [f"--json={tmp / 'incr.json'}"], tmp / "incr.json")

    print("\n== [4/5] 数据契约：主库 fde_rag（干净数据，应全绿）==")
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
    # 判据不再靠人验一次：增量检查第 [11] 步每轮造一次 +1 再收回，下面只复核它给的
    # 三个数（before/after/restored），不看它自己的 ok 字段。缺证据、涨不上去、
    # 收不回来，三种都红。
    prev_rows = None
    if OUT.exists():
        try:
            prev_rows = (json.loads(OUT.read_text(encoding="utf-8")).get("quarantine") or {}).get("rows")
        except Exception:
            prev_rows = None
    delta = (q_rows - prev_rows) if isinstance(prev_rows, int) else None

    # 事实层放最后：它横跨两库（读沙箱当前值、写主库历史），中途会把 stg_devices
    # TRUNCATE 再恢复，放在前面会打乱其他几块对沙箱状态的前提。
    print("\n== [5/5] 事实层：改值留版本、同键拒第二条 ==")
    fa = _run(factschk.main, [f"--json={tmp / 'facts.json'}"], tmp / "facts.json")

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
    elif i.get("duplicated"):
        issues.append("增量装载出现重复 id（落库幂等失效）")
    # 下面几项各自按数字判，不拿 status 当闸门：status 现在同时承载「重复 / 隔离区探针 /
    # 时间轴」三种失败，用一个布尔量概括它们，会让「哪一条红」变成靠猜。
    if i.get("status") != "error":
        # 重叠窗口不能只有声明：policy 说开着，[9] 步就必须真的比 [8] 多捞回来一条
        p = i.get("policy") or {}
        ev = i.get("overlap_evidence") or {}
        if not p.get("window_seconds"):
            issues.append("增量没带重叠窗口口径（policy 缺失或窗口为 0，报告里的窗口没有来源）")
        elif (ev.get("recovered_with_window") or 0) <= (ev.get("leaked_without_window") or 0):
            issues.append("重叠窗口没把漏掉的那条补回来（宣称开着但没生效）")
    if d.get("status") == "error":
        issues.append("删除同步检查没跑起来")
    elif d.get("status") != "ok":
        issues.append("删除同步出现异常判定（误删或该删未删）")
    # 三道护栏任一失效都算红：这是「不报错但删多了」的地方，不能只看 status
    if d.get("status") == "ok":
        guards = d.get("guards") or []
        if len(guards) < 6:
            issues.append("删除同步的护栏少了（应有 6 条）")
    # 指纹漏一列 = 那一列变了永远不重灌，且没有任何症状。表结构漂移只有这里能兜住
    if d.get("status") == "ok":
        hc = d.get("content_hash_cols") or {}
        if hc.get("missing"):
            issues.append(f"内容指纹没纳入 {'/'.join(hc['missing'])}（这些列变了不会触发重灌）")
    # 判定参数必须随产物走：报告里的阈值若被人手写回旧值，读报告的人就按错的规则判断
    if d.get("status") == "ok":
        p = d.get("policy") or {}
        guards = d.get("guards") or []
        if not p.get("absent_confirm") or not p.get("min_snapshot_ratio"):
            issues.append("删除同步没带判定参数（policy 缺失，报告里的阈值没有来源）")
        elif guards and str(p["absent_confirm"]) not in guards[0]:
            issues.append("护栏文案里的阈值与 policy 不一致（有人手写了旧值）")
    if q is None:
        # 表不存在和连不上库在 quarantine_report 里是同一个返回值（None），
        # 但两种都不能当成「隔离区是空的」——读不到不等于没有
        issues.append("隔离区读不到（表不存在或库连不上，不等于 0 条）")
    elif delta is not None and delta > 0:
        issues.append(f"隔离区比上次多了 {delta} 条（有新行被挡在主表外）")
    # 「隔离区涨了会红」这条判据必须每轮有真实输入。增量检查第 [11] 步造一次 +1 再收回，
    # 这里只复核它给的那三个数（见 quarantine_probe_issues）。
    if i.get("status") != "error":
        issues.extend(quarantine_probe_issues(i.get("quarantine_probe") or {}))
    # 时间轴：源端过滤 / 本地水位 / 业务时间必须是同一个钟。登记自相矛盾、或探针
    # 证明「业务时间早于水位的行被灌进来了」，都意味着真源上会整批静默丢。
    if i.get("status") != "error":
        issues.extend(time_axis_issues(i.get("time_axis") or {}))
    # 事实层：改值必须留版本、同键第二条当前值必须被拒。这两条不成立，
    # L1 就只是多了一张表，不是多了一个版本。
    if fa.get("status") == "error":
        issues.append("事实层检查没跑起来")
    else:
        issues.extend(facts_issues(fa))

    payload = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "contract": c,
        "matrix": m,
        "incremental": i,
        "delete": d,
        "facts": fa,
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
            "note": ("绝对值 1 条来自增量检查的固定夹具（id=4、ts=2027-01-01），不是生产脏数据；"
                     "「隔离区涨了会不会被发现」由增量检查第 [11] 步每轮造一次 +1 再收回证明"
                     "（见 incremental.quarantine_probe），所以这里的 delta 只在固定夹具缺席时才不是 0；"
                     "生产库的隔离区请单独跑 check_data_contract.py --dsn=<生产库>"),
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
    ta = i.get("time_axis") or {}
    tp = ta.get("probe") or {}
    print(f"时间轴口径    登记 {'OK' if ta.get('static_ok') else 'FAIL'}，"
          f"探针新增 {tp.get('rows_added')} 条（应为 0），"
          f"业务时间字段 {((ta.get('registry') or {}).get('business_time_field') or '?')}")
    print(f"删除同步      活跃 {d.get('live_devices')} 台 / 软删 {d.get('soft_deleted')} 台 / "
          f"审计 {d.get('audit_rows')} 条 / 护栏 {len(d.get('guards') or [])} 条")
    ft = fa.get("totals") or {}
    fv = fa.get("versioning") or {}
    fu = fa.get("uniqueness") or {}
    fc = fa.get("consistency") or {}
    print(f"事实层        历史 {ft.get('rows')} 行 / 当前 {ft.get('current')} 行，"
          f"改值 1 关 1 开：{fv.get('old_closed')}/{fv.get('new_supersedes_old')}，"
          f"唯一索引拒第二条：{fu.get('rejected')}，"
          f"与当前值一致 {fc.get('checked')} 项（不符 {len(fc.get('mismatched') or [])} 项）")
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
