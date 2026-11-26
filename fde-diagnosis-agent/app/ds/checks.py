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
    """红：业务说已关单（折成「已完成」/「已取消」），看板阶段却还停在未关闭列。

    「已完成但看板未关闭」在自家库上是真红：关单的人忘了拖看板。到了 Odoo 就不能这么判——
    `stage_id` 是看板列、可以随手拖动，不是终态。区别不在数据，在**这条判据的前提**是否成立，
    所以判据本身要能声明「对这家客户不适用」，而不是让跑它的人自己记住。
    """
    board_closed = set(spec.get("board_closed_states") or [])
    closed_labels = set(spec.get("closed_labels") or ["已完成", "已取消"])
    fold = ctx.get("fold") or {}
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
