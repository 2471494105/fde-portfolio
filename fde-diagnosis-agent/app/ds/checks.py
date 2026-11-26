"""判据的实现与执行入口：声明「不适用」，要能被机器证明真的没跑。

为什么要有这个文件
------------------
`status_policy.json` 的 `unsupported_checks` 原先只是一句声明（而且是 id 与散文混在一个
字符串里）。它保证「有人写下了不适用」，保证不了「代码真的没跑」。声明写了、判据照跑，
每天出一批假红；声明写了、判据其实早被删了，又变成另一种装饰——两种都只有人读文档才能发现。

所以把「跑不跑」收成一个入口：`run_check()` 先问 `check_applies()`，不适用就直接返回
`not_applicable` 并且**根本不去读数据**；适用才调用实现。返回值的 `ran` 是「实现有没有被
调用」的诚实信号，E 判据和单测都靠它判，而不是靠人相信。

判据实现保持纯函数：吃 rows 与 ctx（折法、词表），返回 (problems, checked)。不连库、不读
文件，方便拿真数据手工喂进去做反例——「同一批行，去掉声明就立刻红」这种证据最好懂。
"""

from __future__ import annotations

from typing import Callable

from . import status_policy as sp

Impl = Callable[[list[dict], dict, dict], tuple[list[str], int]]


def work_order_state_stage_conflict(rows: list[dict], spec: dict, ctx: dict) -> tuple[list[str], int]:
    """红：业务说已关单（折成闭环词），看板阶段却还停在未关闭列。

    「已完成但看板未关闭」在自家库上是真红：关单的人忘了拖看板。到了 Odoo 就不能这么判——
    `stage_id` 是看板列、可以随手拖动，不是终态。区别不在数据，在**这条判据的前提**是否成立，
    所以判据本身要能声明「对这家客户不适用」，而不是让跑它的人自己记住。

    「已关闭」这个词属于**客户**，不属于判据：判据只认「闭环」这件事，客户用哪个词表达看板关上，
    登记在客户那一份里（`stage_board_closed`）。缺这份词表就抛 `CheckNotConfigured`——
    空词表会被读成「所有 stage 都还开着」，判出一批看着像真问题的假红。
    """
    board_closed = set(ctx.get("stage_board_closed") or [])
    if not board_closed:
        raise sp.CheckNotConfigured(
            f"客户 {ctx.get('client')!r} 适用 stage 冲突判据，但没登记 stage_board_closed——"
            "空词表会把每个 stage 都判成「未关闭」"
        )
    fold = ctx.get("fold") or {}
    # 「关单」这个词也必须是显式的：`已合并` 不算——合并后看板跟着合并到的那张单走，
    # 用它去判这张单「看板没关」是把别人的看板算到它头上。写进 spec 而不是从 closed_states 推，
    # 推出来的会把「已合并」悄悄算进来（第一次改就是这样，红从 19 条变成 28 条，且没人会注意到）。
    closed_labels = set(spec.get("closed_order_labels") or [])
    if not closed_labels:
        raise sp.CheckNotConfigured(
            "判据没登记 closed_order_labels——说不出「哪些闭环词算关单」，红的基础就不存在"
        )
    problems: list[str] = []
    for r in rows:
        state = str(r.get("state") or "")
        stage = str(r.get("stage_id") or "")
        folded = fold.get(state, state)
        if folded in closed_labels and stage and stage not in board_closed:
            problems.append(f"{r.get('wo_no') or '?'}：{state}→{folded}，看板停在 {stage}")
    return problems, len(rows)


def status_vocabulary_coverage(rows: list[dict], spec: dict, ctx: dict) -> tuple[list[str], int]:
    """红：库里出现既不算闭环、也不算已知未闭环的状态。

    这条对**所有**客户适用（它只问「这个词我认识吗」，不问「看板是什么」）。它同时是 E 判据
    的对照组：一家客户可以合理地关掉某条判据，但不该关到「一条都不跑」——留一条与版面无关
    的判据，才能区分「这条不适用」和「这家客户没人管」。
    """
    closed = set(ctx.get("closed_states") or [])
    known = set(ctx.get("known_open_states") or [])
    problems: list[str] = []
    seen: list[str] = []
    for r in rows:
        state = str(r.get("state") or "")
        if state and state not in closed and state not in known:
            seen.append(state)
    uniq = sorted({s for s in seen})
    for s in uniq:
        problems.append(f"库里出现未登记状态 {s!r}——既不算闭环也不算未闭环，会被机械地归进某一类")
    return problems, len(rows)


# id → 实现。id 必须同时登记在 status_policy.json 的 `checks` 里，E 判据两边对账。
_RUNNERS: dict[str, Impl] = {
    "work_order_state_stage_conflict": work_order_state_stage_conflict,
    "status_vocabulary_coverage": status_vocabulary_coverage,
}


def runners() -> set[str]:
    """已实现的判据 id。E 判据拿它和登记表对账：登记了没实现 = 永远不会判出东西。"""
    return set(_RUNNERS)


_SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "out", "node_modules", ".pytest_cache"}


def bypass_problems(root=None) -> list[str]:
    """F 判据：实现只许从 `run_check` 进去。返回「文件:行: 说明」，空列表表示没有绕过。

    为什么必须扫而不是靠约定：E 判据验的是「入口在不适用的客户身上没跑」。一旦有人绕过入口
    直接调实现（`from app.ds.checks import status_vocabulary_coverage`），E 看不见任何异常，
    仍然是绿的——**判据的证明力被一个 import 就绕过去了**。

    扫三类：
      1. `from app.ds.checks import <实现名>` —— 把实现从入口里掏出来；
      2. 直接调用实现名 —— 没有 `check_applies()` 把关；
      3. 访问 `_RUNNERS` —— 自己按 id 取实现，等价于绕过。
    """
    import ast
    from pathlib import Path

    base = Path(root) if root else Path(__file__).resolve().parents[2]
    me = Path(__file__).resolve()
    # 行级字段判据是第二族（app/ds/field_checks.py），同样只许从 fc.run 进去；
    # 分族不是放松约束：实现名一起扫，绕过哪一族都报。
    from . import field_checks as fc

    impl_names = {fn.__name__ for fn in _RUNNERS.values()} | {fn.__name__ for fn in fc.RULES.values()}
    out: list[str] = []

    for path in sorted(base.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.resolve() == me:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            out.append(f"{path.relative_to(base)}：解析失败（{e}）——扫不了不等于没绕过")
            continue
        rel = path.relative_to(base)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in impl_names:
                        out.append(f"{rel}:{node.lineno}：import 判据实现 {alias.name}——"
                                   "绕过 run_check，也绕过 check_applies()")
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id in impl_names:
                    out.append(f"{rel}:{node.lineno}：直接调用 {f.id}()——没有入口把关")
                if isinstance(f, ast.Attribute) and f.attr == "_RUNNERS":
                    out.append(f"{rel}:{node.lineno}：访问 _RUNNERS——按 id 自己取实现等于绕过入口")
            elif isinstance(node, ast.Attribute) and node.attr == "_RUNNERS":
                out.append(f"{rel}:{node.lineno}：访问 _RUNNERS——按 id 自己取实现等于绕过入口")
    return out


def run_check(check_id: str, rows: list[dict] | None = None, name: str | None = None,
              policy: dict | None = None) -> dict:
    """唯一的执行入口。返回里 `ran` 表示实现到底有没有被调用。"""
    p = policy or sp.load()
    spec = sp.checks_registry(p).get(check_id)
    if spec is None:
        raise sp.UnknownCheck(f"判据 {check_id!r} 没登记在 status_policy.json 的 checks 里")

    who = name or p.get("default_client")
    if not sp.check_applies(check_id, who, p):
        # 不适用就别碰数据：连 rows 都不看，返回里也如实写 ran=False。
        # 「读了一遍然后忽略结果」和「根本没读」在报告里长得一样，这里要能区分。
        return {
            "check": check_id,
            "client": who,
            "status": "not_applicable",
            "ran": False,
            "checked": 0,
            "problems": [],
            "reason": sp.unsupported_checks(who, p).get(check_id, ""),
        }

    fn = _RUNNERS.get(check_id)
    if fn is None:
        # 登记了却没人实现：不能返回「ok」，那是把没跑说成通过。
        raise sp.UnknownCheck(
            f"判据 {check_id!r} 已登记但 app/ds/checks.py 里没有实现——"
            "不能返回绿，那是把「没跑」说成「通过」"
        )
    c = sp.client(who, p)
    ctx = {
        "client": who,
        "fold": sp.vocab_fold(who, p),
        "closed_states": list(c.get("closed_states") or []),
        "known_open_states": list(c.get("known_open_states") or []),
        # 客户级词表：判据不自己带词，缺了就抛，不拿默认值糊过去
        "stage_board_closed": list(c.get("stage_board_closed") or []),
    }
    problems, checked = fn(list(rows or []), spec, ctx)
    return {
        "check": check_id,
        "client": who,
        "status": "fail" if problems else "ok",
        "ran": True,
        "checked": checked,
        "problems": problems,
    }
