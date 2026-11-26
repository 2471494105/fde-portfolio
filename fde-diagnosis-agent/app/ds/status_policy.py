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
    if not c.get("registered") or not states:
        raise PolicyNotRegistered(
            f"客户 {c.get('display_name') or name!r} 未登记闭环口径"
            f"（source_of_truth={c.get('source_of_truth')!r}）"
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
