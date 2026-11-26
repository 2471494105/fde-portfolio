# -*- coding: utf-8 -*-
"""T14/T15 验收：判据的「不适用」要被证明，判据的实现要被守住入口。

T14 把「声明不适用」做成可证伪：`run_check` 返回 `ran`，声明不适用的客户必须没跑。
T15 再补一个漏洞：E 判据只看得见走入口的调用。有人绕过入口直调实现，E 就是瞎的——
所以「只许走入口」不能停在约定上，得能扫出来（F 判据，AST 扫全仓库）。

为什么值得单独一个脚本：`unsupported_checks` 原来只是一句话（而且 id 与散文混在一个字符串里）。
一句话能骗过所有人——写得漂亮、评审点头、代码照跑。这里把它拆成三个可证的动作：

  T14-1 可真跑        `client_a` 适用 → `ran=True`、`checked=60`，真判出红（不是空转）
  T14-2 真不跑        `client_b_odoo` 声明不适用 → `ran=False`、`checked=0`，且**传 rows=None 也不崩**
                      （证明它压根没读数据，不是读了再忽略）
  T14-3 承重          同一批 60 行、只把声明去掉 → 立刻判出 19 条红。所以「不适用」不是装饰，
                      它挡住的正是每天 19 条假红
  T14-4 声明的形状    v2 的字符串数组 / 登记表里不存在的 id / 空理由 / 一条都不跑 → E 判据都要报
  T14-5 判据自己会红  把入口换成「永远 ran=True」，E 必须报「声明没落到代码里」

跑法（在 fde-diagnosis-agent 目录）：
    python -X utf8 scripts/verify_unsupported.py
前置：`fde_client` 可连（postgres/postgres），60 行 `public.biz_work_order`。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DSN = "postgresql://postgres:postgres@localhost:5432/fde_client"
CHECK = "work_order_state_stage_conflict"
COVER = "status_vocabulary_coverage"


def load_rows(dsn: str = DSN) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT wo_no, state, stage_id, closed_at FROM public.biz_work_order ORDER BY wo_no")
        return [dict(r) for r in cur.fetchall()]


def main() -> int:
    from app.ds import checks as ck
    from app.ds import status_policy as sp

    sys.path.insert(0, str(ROOT / "scripts"))
    import check_status_policy as csp

    ok = True

    def chk(name: str, cond: bool, note: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(("[ok] " if cond else "[!!] ") + name + (("  " + note) if note else ""))

    policy = sp.load()
    rows = load_rows()
    print(f"== 取数：fde_client 60 行（实际 {len(rows)} 行）")
    chk("T14-0 行数 60", len(rows) == 60)

    print("== T14-1 适用 → 真跑，并真判出红")
    a = ck.run_check(CHECK, rows=rows, name="client_a", policy=policy)
    print(f"  client_a：status={a['status']} ran={a['ran']} checked={a['checked']} 问题 {len(a['problems'])} 条")
    for line in a["problems"][:3]:
        print("    · " + line)
    chk("T14-1 ran=True", a["ran"] is True)
    chk("T14-1 读数了 60 行", a["checked"] == 60)
    chk("T14-1 真判出红（19 条：已完成 10 + 已取消 9）", len(a["problems"]) == 19, f"实得 {len(a['problems'])}")

    print("== T14-2 声明不适用 → 真的没跑（连数据都不碰）")
    b = ck.run_check(CHECK, rows=rows, name="client_b_odoo", policy=policy)
    print(f"  client_b_odoo：status={b['status']} ran={b['ran']} checked={b['checked']} 理由={b['reason'][:34]}…")
    chk("T14-2 status=not_applicable", b["status"] == "not_applicable")
    chk("T14-2 ran=False", b["ran"] is False)
    chk("T14-2 checked=0", b["checked"] == 0)
    chk("T14-2 问题为空", b["problems"] == [])
    b_none = ck.run_check(CHECK, rows=None, name="client_b_odoo", policy=policy)
    chk("T14-2 传 rows=None 也不崩（证明没读数据）", b_none["status"] == "not_applicable")

    print("== T14-3 承重：只把声明去掉，同一批行立刻红")
    stripped = {
        **policy,
        "clients": {
            **policy["clients"],
            "client_b_odoo": {**policy["clients"]["client_b_odoo"], "unsupported_checks": {},
                              # 去掉声明后判据就真的跑起来了——它要客户级的看板关闭词，
                              # 没登记会抛 CheckNotConfigured（T16）。这里显式补上，才能看出「红了几条」。
                              "stage_board_closed": ["已关闭"]},
        },
    }
    b2 = ck.run_check(CHECK, rows=rows, name="client_b_odoo", policy=stripped)
    print(f"  去掉声明后：status={b2['status']} ran={b2['ran']} 问题 {len(b2['problems'])} 条")
    for line in b2["problems"][:3]:
        print("    · " + line)
    chk("T14-3 去掉声明就红", b2["status"] == "fail")
    chk("T14-3 红的是 19 条（不是 0 条也不是 60 条）", len(b2["problems"]) == 19, f"实得 {len(b2['problems'])}")

    print("== T14-4 声明的形状：四种错法都要报")
    v2_list = {
        **policy,
        "clients": {**policy["clients"],
                    "client_b_odoo": {**policy["clients"]["client_b_odoo"],
                                      "unsupported_checks": [CHECK + "（理由写在括号里）"]}},
    }
    chk("T14-4 v2 字符串数组要报", any("v2 字符串数组" in p for p in sp.check_problems(v2_list)))

    ghost = {
        **policy,
        "clients": {**policy["clients"],
                    "client_b_odoo": {**policy["clients"]["client_b_odoo"],
                                      "unsupported_checks": {CHECK: "理由", "stage_conflict_v1": "旧名字"}}},
    }
    probs = sp.check_problems(ghost)
    chk("T14-4 登记表里没有的 id 要报（声明没人能兑现）",
        any("stage_conflict_v1" in p and "没有这个 id" in p for p in probs))

    no_reason = {
        **policy,
        "clients": {**policy["clients"],
                    "client_b_odoo": {**policy["clients"]["client_b_odoo"],
                                      "unsupported_checks": {CHECK: "   "}}},
    }
    chk("T14-4 空理由要报", any("没写理由" in p for p in sp.check_problems(no_reason)))

    all_off = {
        **policy,
        "clients": {**policy["clients"],
                    "client_b_odoo": {**policy["clients"]["client_b_odoo"],
                                      "unsupported_checks": {CHECK: "不适用", COVER: "也不适用"}}},
    }
    chk("T14-4 一条都不跑要报",
        any("把所有登记判据都声明不适用" in p for p in sp.check_problems(all_off)))

    print("== T14-5 E 判据自己会红：把入口换成「永远 ran=True」")
    real = ck.run_check

    def always_run(check_id, rows=None, name=None, policy=None):
        return {"check": check_id, "client": name, "status": "ok", "ran": True,
                "checked": 0, "problems": []}

    ck.run_check = always_run
    try:
        res = csp.check_unsupported(policy)
    finally:
        ck.run_check = real
    print(f"  注入假入口后 E：{res['status']}，{len(res['detail'])} 条")
    for line in res["detail"][:3]:
        print("    · " + line)
    chk("T14-5 假入口被 E 抓住", res["status"] == "fail"
        and any("声明不适用，但入口仍然跑了" in d for d in res["detail"]))

    print("== T14-6 端到端：五条判据（B 客户）")
    env = dict(os.environ, FDE_STATUS_CLIENT="client_b_odoo", PYTHONIOENCODING="utf-8")
    import subprocess
    r = subprocess.run([sys.executable, "-X", "utf8", "scripts/check_status_policy.py", "--skip-db"],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
    print("  " + "\n  ".join(l for l in (r.stdout or "").strip().splitlines()))
    chk("T14-6 E ok 且 exit 0", r.returncode == 0 and "E_unsupported_checks" in r.stdout
        and "[ok     ] E_unsupported_checks" in r.stdout)

    print("== T15 绕过入口的扫描：正常仓库 0 命中，注入影子文件必须被抓")
    import tempfile
    import textwrap

    real_scan = ck.bypass_problems(ROOT)
    chk("T15 全仓库没有绕过（0 命中）", real_scan == [], f"实得 {len(real_scan)} 条")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "app" / "ds"
        d.mkdir(parents=True)
        (Path(td) / "app" / "__init__.py").write_text("", encoding="utf-8")
        (d / "__init__.py").write_text("", encoding="utf-8")
        (d / "clean.py").write_text(
            "from app.ds import checks as ck\n"
            "def go(rows):\n"
            "    return ck.run_check('status_vocabulary_coverage', rows=rows)\n",
            encoding="utf-8")
        chk("T15 走入口的写法不误报", ck.bypass_problems(td) == [])
        (d / "rogue.py").write_text(textwrap.dedent("""
            from app.ds.checks import status_vocabulary_coverage
            def go(rows, spec, ctx):
                return status_vocabulary_coverage(rows, spec, ctx)
        """), encoding="utf-8")
        hits = ck.bypass_problems(td)
        for h in hits:
            print("    · " + h)
        chk("T15 直接 import + 调用都被抓", len(hits) >= 2, f"实得 {len(hits)} 条")
        chk("T15 指到影子文件", any("rogue.py" in h for h in hits))

    print("== T16 看板关闭词属于客户：换词表，数字必须跟着变")
    words_swapped = {
        **policy,
        "clients": {**policy["clients"],
                    "client_a": {**policy["clients"]["client_a"],
                                 "stage_board_closed": ["进行中"]}},
    }
    a2 = ck.run_check(CHECK, rows=rows, name="client_a", policy=words_swapped)
    print(f"  把 client_a 的看板关闭词改成 ['进行中']：红 {len(a2['problems'])} 条")
    chk("T16 换词表后 19 → 15 条", len(a2["problems"]) == 15, f"实得 {len(a2['problems'])}")

    no_words = {
        **policy,
        "clients": {**policy["clients"],
                    "client_a": {**policy["clients"]["client_a"], "stage_board_closed": []}},
    }
    try:
        ck.run_check(CHECK, rows=rows, name="client_a", policy=no_words)
        chk("T16 缺词表必须抛 CheckNotConfigured", False, note="它没抛")
    except sp.CheckNotConfigured as e:
        print(f"  缺词表时抛：{e}")
        chk("T16 缺词表必须抛 CheckNotConfigured", True)
    chk("T16 E 静层也报「适用却缺词表」",
        any("stage_board_closed" in p and "没登记" in p for p in sp.check_problems(no_words)))

    merged = [{"wo_no": "M1", "state": "已合并", "stage_id": "进行中"}]
    m = ck.run_check(CHECK, rows=merged, name="client_a", policy=policy)
    print(f"  已合并 + 看板未关：{len(m['problems'])} 条（closed_order_labels 不含已合并）")
    chk("T16 已合并不算关单（否则红从 19 变 28）", m["problems"] == [], f"实得 {m['problems']}")

    print("== T17 字段面：priority / 班组 必须二选一（折法，或带理由地声明不可用）")
    chk("T17 现有策略 G 判据为空", sp.field_fold_problems(policy) == [],
        note=str(sp.field_fold_problems(policy)))
    a_fold = sp.field_fold("client_a", policy)
    print(f"  client_a 折法：priority {sorted(a_fold['priority'])} / team {sorted(a_fold['maintenance_team_id'])}")

    dropped = {
        **policy,
        "clients": {**policy["clients"],
                    "client_b_odoo": {**policy["clients"]["client_b_odoo"],
                                      "field_fold": {},
                                      "unsupported_columns": [],
                                      "unsupported_columns_why": {}}},
    }
    dp = sp.field_fold_problems(dropped)
    for h in dp:
        print("    · " + h)
    chk("T17 两条都撤掉 → 两个字段都报", len(dp) == 2, f"实得 {len(dp)} 条")

    no_reason = {
        **policy,
        "clients": {**policy["clients"],
                    "client_b_odoo": {**policy["clients"]["client_b_odoo"],
                                      "unsupported_columns_why": {}}},
    }
    chk("T17 声明不可用却没理由 → 报",
        any("没写理由" in p for p in sp.field_fold_problems(no_reason)),
        note=str(sp.field_fold_problems(no_reason)))

    # 折法得对得上真数据：A 侧的观测值必须全在折法键里，否则折法只是纸面文章。
    import psycopg

    with psycopg.connect(DSN) as conn:
        observed = {
            "priority": {str(r[0]) for r in conn.execute(
                "select distinct priority from public.biz_work_order").fetchall()},
            "maintenance_team_id": {str(r[0]) for r in conn.execute(
                "select distinct maintenance_team_id from public.biz_work_order").fetchall()},
        }
    for f, vals in observed.items():
        print(f"  真库 {f} 观测值 {sorted(vals)}")
        chk(f"T17 折法覆盖真库 {f} 的全部观测值",
            vals == set(a_fold[f]), note=f"差集 {vals - set(a_fold[f])}")
    chk("T17 折法是恒等（不偷偷改语义）",
        all(a_fold[f][v] == v for f, vals in observed.items() for v in vals))

    print("== T18 counterpart 不再是文本：页面标红的行必须是判据说红的那批工单")
    from app.ds import counterpart as cpt

    js, why = cpt._run_js(cpt.REPO_ROOT / "mock-mes/api/static/flag.js", rows)
    py = ck.run_check(CHECK, rows=rows, name="client_a", policy=policy)
    py_ids = {x.split("：")[0] for x in py["problems"]}
    js_ids = set(js["judge"]["red"])
    print(f"  页面 red {len(js_ids)} / amber {len(js['judge']['amber'])}；判据 red {len(py_ids)}")
    chk("T18 两侧红数相同（19）", len(js_ids) == len(py_ids) == 19, f"页面 {len(js_ids)} 判据 {len(py_ids)}")
    chk("T18 两侧工单号集合相同", js_ids == py_ids,
        note=f"页面多 {sorted(js_ids - py_ids)[:4]}，判据多 {sorted(py_ids - js_ids)[:4]}")
    chk("T18 现有策略 H 为空", cpt.counterpart_problems(policy, rows=rows) == [],
        note=str(cpt.counterpart_problems(policy, rows=rows))[:200])
    amber = js["judge"]["amber"]
    print(f"  页面 amber {len(amber)} 张「关板未关单」——登记为 extra_rules，Python 侧没有对应判据")
    chk("T18 已登记的差异在真数据上确实存在（6 张）", len(amber) == 6, f"实得 {len(amber)}")
    chk("T18 登记过的差异不算问题（H 仍为空）",
        cpt.counterpart_problems(policy, rows=rows) == [])

    spec = sp.checks_registry(policy)[CHECK]
    drifted = {**policy, "checks": {**policy["checks"],
                                    CHECK: {**spec, "closed_order_labels": ["已完成"]}}}
    msg = cpt.counterpart_problems(drifted, rows=None)
    for m in msg:
        print("    · " + m)
    chk("T18 词表漂移被报（页只认已完成、判据认两个）", any("各写一份词" in m for m in msg))

    # 条件漂移要用临时副本：真文件不能改，改了就不是反例而是事故
    import shutil, tempfile
    tmp = Path(tempfile.mkdtemp(prefix="t18_"))
    (tmp / "mock-mes/api/static").mkdir(parents=True)
    (tmp / "mock-mes/scripts").mkdir(parents=True)
    src = (cpt.REPO_ROOT / "mock-mes/api/static/flag.js").read_text(encoding="utf-8")
    mutated = src.replace("W.boardClosed.indexOf(r.stage_id) < 0", "W.boardClosed.indexOf(r.stage_id) >= 0")
    assert mutated != src, "T18 反例：条件没被改到，反例就假了"
    (tmp / "mock-mes/api/static/flag.js").write_text(mutated, encoding="utf-8")
    shutil.copy2(cpt.REPO_ROOT / "mock-mes/scripts/verify_ui.py", tmp / "mock-mes/scripts/verify_ui.py")
    msg2 = cpt.counterpart_problems(policy, rows=rows, root=tmp)
    for m in msg2:
        print("    · " + m)
    chk("T18 条件漂移被动态对账抓到", any("对不上同一批工单" in m for m in msg2),
        note=str(msg2)[:200])
    shutil.rmtree(tmp, ignore_errors=True)

    plain = {**policy, "checks": {**policy["checks"], CHECK: {**spec, "counterpart": "index.html 的 flag"}}}
    chk("T18 回到纯文本要报", any("纯文本" in m for m in cpt.counterpart_problems(plain, rows=None)))

    orig_node = cpt._node
    cpt._node = lambda: None
    try:
        msg3 = cpt.counterpart_problems(policy, rows=rows)
    finally:
        cpt._node = orig_node
    print("    · " + (msg3[0] if msg3 else "（没报，说明 node 缺失被静默当成通过）"))
    chk("T18 node 缺失要报，不能当通过", any("跑不了就是没验" in m for m in msg3))

    print("\nRESULT:", "ALL PASS" if ok else "HAS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
