"""客户口径登记：谁家的「闭环」由谁定义。

为什么单独一个文件，而不是继续把手抄的字面量散在各处
----------------------------------------------------
状态集是**契约的一个版本**，不是字典。OPC UA 把 `FiniteStateMachineType`
定义成封闭集合：子类型不得新增、也不得删除 States / Transitions。换一家客户
等于换一版契约，必须登记、必须能拒跑，而不是在 `IN (...)` 里再塞一个词。

换客户时的三条后果，都实测过：
  1. 口径漏改 → 保护集失效 → 删除功能等于没有，且**不报错**；
  2. 口径改宽 → 已闭环的单继续占幂等键 → 同一问题处理完再坏，建不了新单；
  3. 未登记就猜 → 客户给的 `state`/`stage_id` 被当成「未闭环」，保护集只增不减。

所以本模块只做三件事：读登记、给出参数化的 SQL 片段、在未登记时**抛错**。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "data" / "mapping" / "status_policy.json"

# 临时切客户用（换客户实测、反例自证）。不设就按登记表里的 default_client。
CLIENT_ENV = "FDE_STATUS_CLIENT"


class PolicyNotRegistered(RuntimeError):
    """该客户没登记闭环口径。

    遇到它就**停**，不要猜一个默认值。理由与 Airbyte 对「游标/主键被删」的处理
    一致：破坏性变更暂停等人工，而不是让下游自己挑一个能跑下去的默认。
    """


def load(path: Path | str | None = None) -> dict:
    """读登记表。环境变量 `FDE_STATUS_CLIENT` 可临时切客户。

    切客户只改这一处默认值，不给任何调用点留「顺手传个词表」的口子。
    """
    p = Path(path) if path else POLICY_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    forced = os.environ.get(CLIENT_ENV)
    if forced:
        data = {**data, "default_client": forced}
    return data


def client(name: str | None = None, policy: dict | None = None) -> dict:
    p = policy or load()
    name = name or p.get("default_client")
    try:
        return p["clients"][name]
    except KeyError as e:
        raise PolicyNotRegistered(f"客户 {name!r} 没有口径登记：{POLICY_PATH}") from e


def closed_states(name: str | None = None, policy: dict | None = None) -> tuple[str, ...]:
    """闭环（终态）词表。未登记或为空一律抛错，不返回空元组。

    返回空元组会静默把「所有单都未闭环」当成结论——正是我们要拒的那种猜法。
    """
    c = client(name, policy)
    states = c.get("closed_states")
    if not c.get("registered"):
        raise PolicyNotRegistered(
            f"客户 {c.get('display_name') or name!r} 未登记闭环口径"
            f"（source_of_truth={c.get('source_of_truth')!r}）"
        )
    if not states:
        # 登记了却给了空词表，跟「没登记」是两件事：一个要人补登记，一个要人补词表。
        # 报错混用会让修的人找错地方（K-5 反例第一次跑出来就是这样）。
        raise PolicyNotRegistered(
            f"客户 {c.get('display_name') or name!r} 已登记但闭环词表为空——"
            "空词表会被下游当成「全部未闭环」，等于删除功能静默消失"
        )
    return tuple(states)


def open_predicate(field: str = "status", name: str | None = None,
                   policy: dict | None = None) -> tuple[str, tuple]:
    """「未闭环」的 SQL 片段与参数。返回 (片段, 参数)，调用方自己拼进查询。

    用参数而不是字面量：同一份口径只在这里展开一次，调用点抄一次就多一个
    会漂的地方（本项目 2026-11-26 之前有四处各写一份）。
    """
    states = closed_states(name, policy)
    return f"{field} <> ALL(%s::text[])", (list(states),)


def sql_in_list(states: tuple[str, ...]) -> str:
    """把词表展开成 SQL 的 `IN (...)` 字面量。

    只给「SQL 是字符串模板」的场合用（如 `check_data_contract.py` 的断言表）。
    来源仍然是本模块，所以漂移只有一处可漂。
    """
    return ", ".join("'" + s.replace("'", "''") + "'" for s in states)


def policy_summary(name: str | None = None, policy: dict | None = None) -> dict:
    """写进报告/巡检 JSON 的口径摘要。报告里的词表必来自这里，不许手抄。"""
    p = policy or load()
    c = client(name, p)
    return {
        "client": name or p.get("default_client"),
        "version": p.get("version"),
        "registered": bool(c.get("registered")),
        "closed_states": list(c.get("closed_states") or []),
        "known_open_states": list(c.get("known_open_states") or []),
        "priority_dropped": c.get("priority_dropped"),
        "unsupported_columns": list(c.get("unsupported_columns") or []),
        "source_of_truth": c.get("source_of_truth"),
        # 登记簿里都有谁：报告要能回答「这套口径对哪几家是敢用的」。
        "clients": [
            {"name": n, "registered": bool(v.get("registered"))}
            for n, v in sorted(p.get("clients", {}).items())
        ],
    }


# ---------- 漂移判据（纯函数，方便单测；不连库） ----------

_PRED_RE = re.compile(r"status NOT IN\s*\(([^)]*)\)", re.IGNORECASE)


def schema_predicate_states(sql_text: str) -> tuple[str, ...]:
    """从 `data/schema.sql` 里抓出部分唯一索引的闭环词表。

    为什么需要它：索引谓词**必须**是可索引的表达式，读不了 JSON，所以这一处
    只能留字面量。既然留了，就必须有一条判据盯着它别和登记漂开。
    """
    m = _PRED_RE.search(sql_text)
    if not m:
        return ()
    raw = m.group(1)
    items = [s.strip().strip("'\"") for s in raw.split(",")]
    return tuple(s for s in items if s)


def drift(schema_states: tuple[str, ...], policy_states: tuple[str, ...]) -> list[str]:
    """返回漂移描述；空列表表示一致。"""
    out = []
    only_schema = sorted(set(schema_states) - set(policy_states))
    only_policy = sorted(set(policy_states) - set(schema_states))
    if only_schema:
        out.append(f"schema.sql 里有、口径表里没有：{only_schema}"
                   "（索引还在拿旧词判闭环，保护集会与登记不一致）")
    if only_policy:
        out.append(f"口径表里有、schema.sql 里没有：{only_policy}"
                   "（索引不会把这些词算成闭环，已关闭的单仍占幂等键）")
    if not schema_states:
        out.append("schema.sql 里没找到 `status NOT IN (...)` 谓词——判据失去对象")
    return out


def unknown_states(actual: tuple[str, ...], c: dict) -> list[str]:
    """库里出现、但口径表既没算闭环也没算未闭环的状态。

    这种状态最危险：它不属于任何一类，判「闭环」和「未闭环」都说得通，
    而代码只会按词表机械地判——于是它悄悄落进某一类，没人知道是哪一类。
    """
    known = set(c.get("closed_states") or []) | set(c.get("known_open_states") or [])
    return sorted(set(actual) - known)


# ---------- 折法判据（K-5）：换了客户，词不同，事实必须还是同一件事 ----------

def vocab_fold(name: str | None = None, policy: dict | None = None) -> dict:
    """客户词 → 我方基准词的折法。未登记客户照旧拒跑，不返回空表。"""
    c = client(name, policy)
    if not c.get("registered"):
        raise PolicyNotRegistered(f"客户 {c.get('display_name') or name!r} 未登记，没有折法可谈")
    return dict(c.get("vocab_fold") or {})


def fold_problems(name: str | None = None, policy: dict | None = None) -> list[str]:
    """D 判据：折法完整性。空列表表示这家客户的折法说得清。

    为什么需要它：`closed_states` 只说了「哪些词算闭环」，没说「这些词对应我方哪一态」。
    少这一层，两个后果都不会报错——
      · 对方的 done 没折，保护集把它当未闭环 → 删除功能等于没有（静默）；
      · 折到基准词表之外（笔误），下游按字符串比就永远匹配不上 → 同一后果。
    以及反向的一条：我方基准里有、这家客户没有的态，必须在 `fold_gaps_states` 里
    显式认领（比如 Odoo 没有「已合并」）。**不认领就等于假装它存在。**
    """
    p = policy or load()
    c = client(name, p)
    canonical = list(p.get("canonical_states") or [])
    fold = c.get("vocab_fold") or {}
    declared_gaps = set(c.get("fold_gaps_states") or [])
    words = list(c.get("closed_states") or []) + [
        w for w in (c.get("known_open_states") or []) if not w.endswith(":*")
    ]
    out: list[str] = []
    for w in words:
        target = fold.get(w)
        if not target:
            out.append(f"{w} 没有折法目标——它会被机械地归进某一类，没人知道是哪一类")
        elif canonical and target not in canonical:
            out.append(f"{w} 折到 {target!r}，不在我方基准词表 {canonical} 里（多半是笔误）")
    covered = {v for v in fold.values() if v in canonical}
    for s in canonical:
        if s not in covered and s not in declared_gaps:
            out.append(f"我方基准词 {s!r} 在这家客户身上没有落点，也没在 fold_gaps_states 里认领")
    for s in sorted(declared_gaps - set(canonical)):
        out.append(f"fold_gaps_states 里的 {s!r} 不在我方基准词表里（认领错了对象）")
    return out


# ---------- 「不适用」判据（T14）：说过的话，要能被机器对上 ----------

class UnknownCheck(KeyError):
    """判据 id 不在顶层 `checks` 登记表里。

    声明一个登记表里没有的 id，跟写一句「这条对你不适用」的散文没区别：
    没人能兑现它，判据也没法验证它真的没跑。
    """


def checks_registry(policy: dict | None = None) -> dict:
    """顶层判据登记表：id → what / reads / enforced_where / 判据用到的词表。"""
    return dict((policy or load()).get("checks") or {})


def unsupported_checks(name: str | None = None, policy: dict | None = None) -> dict:
    """客户声明「这条判据对我这不适用」：id → 理由。

    v2 的写法把 id 和散文塞进同一个字符串（`"xxx（我方…）：理由…"`），机器根本匹配不上——
    那种「声明」在机器眼里等于零，只能靠人朗读。所以旧写法**显式拒绝**，不做静默兼容：
    兼容等于把「只有声明」的旧状态再养一遍。
    """
    c = client(name, policy)
    if not c.get("registered"):
        raise PolicyNotRegistered(f"客户 {c.get('display_name') or name!r} 未登记，谈不上哪条判据不适用")
    raw = c.get("unsupported_checks") or {}
    if isinstance(raw, list):
        raise PolicyNotRegistered(
            f"客户 {c.get('display_name') or name!r} 的 unsupported_checks 还是 v2 的字符串数组，"
            "id 与理由混在一起无法机器匹配——改成 {check_id: 理由}"
        )
    return dict(raw)


def check_applies(check_id: str, name: str | None = None, policy: dict | None = None) -> bool:
    """这条判据对这家客户适不适用。判据 id 本身必须已登记。"""
    p = policy or load()
    if check_id not in checks_registry(p):
        raise UnknownCheck(f"判据 {check_id!r} 没登记在 {POLICY_PATH} 的 checks 里")
    return check_id not in unsupported_checks(name, p)


def check_problems(policy: dict | None = None) -> list[str]:
    """E 判据：声明与登记必须对得上，且「不适用」不许把判据整体架空。

    报六类：
      1. `checks` 登记表为空 —— 那就没有 id 可匹配，`unsupported_checks` 退化成散文；
      2. 某条登记判据缺 what / reads / enforced_where —— 说不出它读什么、在哪跑；
      3. 登记了但 `app/ds/checks.py` 里没有实现 —— 永远不会判出任何东西；
      4. 客户声明了一个登记表里不存在的 id —— 声明了一句没人能兑现的话；
      5. 声明了却不写理由 —— 与「随手关掉」没有区别；
      6. 全部客户都对同一条判据声明不适用 —— 它永远不会运行，是装饰。
    """
    p = policy or load()
    reg = checks_registry(p)
    out: list[str] = []
    if not reg:
        out.append("checks 登记表为空——unsupported_checks 无从机器匹配，等于只有声明")
    for cid, spec in reg.items():
        for k in ("what", "reads", "enforced_where"):
            if not spec.get(k):
                out.append(f"判据 {cid!r} 缺 {k}：说不出它做什么、读什么、在哪跑")
    # 登记了却没人实现：它永远不会判出任何东西，跟「被声明关掉」是两回事，但同样安静。
    from . import checks as _ck  # 延迟导入，避免模块循环

    implemented = _ck.runners()
    for cid in reg:
        if cid not in implemented:
            out.append(f"判据 {cid!r} 登记了但 app/ds/checks.py 里没有实现——永远不会判出任何东西")
    applies_any: set[str] = set()
    for name, c in sorted(p.get("clients", {}).items()):
        if not c.get("registered"):
            continue
        raw = c.get("unsupported_checks") or {}
        if isinstance(raw, list):
            out.append(f"{name}：unsupported_checks 仍是 v2 字符串数组（id 与理由混在一起，无法机器匹配）")
            continue
        for cid, reason in sorted(raw.items()):
            if cid not in reg:
                out.append(f"{name} 声明 {cid!r} 不适用，但 checks 登记表里没有这个 id——"
                           "声明了一个没人能兑现的判据")
            if not str(reason or "").strip():
                out.append(f"{name} 声明 {cid!r} 不适用却没写理由——与随手关掉没区别")
        applies = set(reg) - set(raw)
        if reg and not applies:
            out.append(f"{name} 把所有登记判据都声明不适用——这家客户身上没有任何一条判据在跑")
        applies_any |= applies
    if reg and not applies_any:
        out.append("所有登记客户都对每条判据声明了不适用：这条判据永远不跑，是装饰")
    return out
