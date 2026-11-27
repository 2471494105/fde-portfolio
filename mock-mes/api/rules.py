#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""判据的规矩状态：哪些红是数据本身错，哪些红是我们自己推的口径。

为什么需要这一层
----------------
判据有两种性质，混在一个总数里就说不清：

- `hard`：数据本身错（时间不可逆、置信度 150、日期在未来），客户不需要点头；
- `assumed`：我方推断的业务规矩（工位要不要全码、三级报警算不算要人管），
  客户从来没答过。这类红**必须**与确定的红分开显示，否则客户第一眼看到 407
  会以为"你判错了 402 行"，其实是"这 402 行在等你回答两道题"。

读数与新鲜度同一套规矩（`obs.py::load_freshness_spec` 是先例）：单一源在
fde 侧的 `data/mapping/check_rules.json`，页面与判据读同一份；读不到就抛，
**不内置兜底清单**——兜底会悄悄变回第二套口径。

没登记的判据不隐藏
------------------
`status_of()` 对没登记的 check_id 返回 `unregistered` 且 `counts_in_total=True`：
宁可多报一行红，也不让一条新判据的红在总数里静默消失。缺登记这件事由
`fde-diagnosis-agent/scripts/verify_check_rules.py` 拦在合并前，不靠运行时兜。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

RULES_PATH = (Path(__file__).resolve().parents[2] / "fde-diagnosis-agent"
              / "data" / "mapping" / "check_rules.json")

_UNREGISTERED = {
    "check_id": None,
    "status": "unregistered",
    "unit": "rows",
    "counts_in_total": True,
    "basis": "该判据未在 check_rules.json 登记（先登记，再由 verify_check_rules 放行）",
    "source": None,
}


@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict[str, dict]:
    p = Path(path) if path else RULES_PATH
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
        return {c["check_id"]: c for c in spec["checks"]}
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"判据规矩登记读取失败（{p}）：{type(e).__name__}: {e}") from e


def status_of(check_id: str) -> dict:
    """一条判据的规矩状态；没登记就标 unregistered 并照旧计数（不静默丢红）。"""
    hit = load().get(check_id)
    if hit:
        return hit
    return {**_UNREGISTERED, "check_id": check_id}


def summary() -> dict:
    """给页面用的小结：几道 hard、几道 assumed、几道未登记。"""
    rows = list(load().values())
    return {
        "checks": len(rows),
        "hard": sum(1 for r in rows if r.get("status") == "hard"),
        "assumed": sum(1 for r in rows if r.get("status") == "assumed"),
        "registered": sum(1 for r in rows if r.get("status") == "registered"),
        "path": str(RULES_PATH),
    }
