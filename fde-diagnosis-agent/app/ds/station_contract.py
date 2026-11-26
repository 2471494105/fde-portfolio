"""工位契约：明细表必须交得出「哪个工位」，后端只查字典，不推导。

为什么不做反推
--------------
现场数据有三种写法：工单 `station_no` = 「1号工位」、缺陷 `work_station` 里混着
「产线A-01」和「4号线」、主数据是「产线A-01」。想「接上」最省事的办法是用
`md_device.work_station` 反推——设备台账知道哪台设备在哪个工位。但实测 60 张
工单里只有 20 张与设备台账一致，40 张矛盾（WO-0002 写「2号工位」，它那台
CX-102 却在产线A-01 = 第 1 工位）。

用设备反推 = 拿一个同样可疑的字段去修另一个字段，还把结果写成事实。这不是
清洗，是替客户编数据。所以本模块只做三件事：

  1. 声明契约：明细必须给 `station_id`，或给（`prod_line` + 工位文本）；
  2. 查字典：能唯一匹配就通过，匹配不到 / 匹配多条都判红，**不猜**；
  3. 把「缺什么」交出去：违约行 + 认领缺口，落到整改单，由产线补。

判断依据只有 `md_station` 一张表。主数据缺失、id 不合规、id 重复，都先判红——
契约本身坏了，下游再对也没意义。
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "data" / "mapping" / "data_contract.json"

_DIGITS = re.compile(r"\d+")


class ContractNotRegistered(RuntimeError):
    """契约或缺口没登记。遇到就停，不猜默认值。"""


def load(path: Path | str | None = None) -> dict:
    p = Path(path) if path else CONTRACT_PATH
    if not p.exists():
        raise ContractNotRegistered(f"契约登记表不存在：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def contracts(registry: dict) -> list[dict]:
    got = registry.get("contracts") or []
    if not got:
        raise ContractNotRegistered("契约表里一条契约都没有")
    return got


def contract_by_id(registry: dict, cid: str) -> dict:
    for c in contracts(registry):
        if c.get("id") == cid:
            return c
    raise ContractNotRegistered(f"契约 {cid!r} 没登记：{CONTRACT_PATH}")


def station_maps(master_rows: list[dict]) -> dict:
    """(station_id → 行) 与 (line_no, seq) → station_id 两份字典。

    两份都要：一份校验 id 本身，一份把「产线 + 工位号」解析成 id。第二份若出现
    (line_no, seq) 重复，说明主数据自己违反 UNIQUE(line_no, seq)，由
    check_station_key 报红，resolve 里按「多条」处理。
    """
    by_id: dict[str, dict] = {}
    by_code: dict[str, dict] = {}
    by_line_seq: dict[tuple[str, int], list[str]] = {}
    for r in master_rows:
        sid = (r.get("station_id") or "").strip()
        if not sid:
            continue
        by_id[sid] = r
        code = (r.get("station_no") or "").strip()
        if code:
            by_code[code] = r
        line = (r.get("line_no") or "").strip()
        seq = r.get("seq")
        if line and seq is not None:
            by_line_seq.setdefault((line, int(seq)), []).append(sid)
    return {"by_id": by_id, "by_code": by_code, "by_line_seq": by_line_seq, "rows": len(master_rows)}


def check_station_key(registry: dict, master_rows: list[dict]) -> list[str]:
    """契约自己的完整性：列在不在、id 全不全、符不符合 pattern、有没有重复。"""
    key = registry.get("station_key") or {}
    problems: list[str] = []
    if not master_rows:
        problems.append("md_station 一行都没有：工位契约没有字典可查")
        return problems

    if "station_id" not in master_rows[0]:
        problems.append("md_station 没有 station_id 列：契约字段在库上不存在（先跑 sql/011_station_id_upgrade.sql）")
        return problems

    pattern = re.compile(key.get("id_pattern") or r".*")
    if not key.get("id_pattern"):
        problems.append("契约没登记 station_id 的 id_pattern")
    blank = [r for r in master_rows if not (r.get("station_id") or "").strip()]
    if blank:
        problems.append(f"md_station 有 {len(blank)} 行 station_id 为空：稳定键不完整")

    bad = [r for r in master_rows if (r.get("station_id") or "").strip() and not pattern.match(str(r["station_id"]).strip())]
    if bad:
        samples = ", ".join(str(r.get("station_no")) for r in bad[:3])
        problems.append(f"md_station 有 {len(bad)} 行 station_id 不符合 {pattern.pattern}（如 {samples}）")

    seen: dict[str, int] = {}
    for r in master_rows:
        sid = (r.get("station_id") or "").strip()
        if sid:
            seen[sid] = seen.get(sid, 0) + 1
    dup = [sid for sid, n in seen.items() if n > 1]
    if dup:
        problems.append(f"md_station 的 station_id 有重复：{', '.join(dup[:3])}")

    expected = key.get("expected_rows")
    if expected is not None and len(master_rows) != int(expected):
        problems.append(f"md_station 行数 {len(master_rows)} != 登记的 {expected}：工位集合变了，契约要跟着改")

    return problems


def resolve(row: dict, contract: dict, maps: dict) -> tuple[str | None, str]:
    """把一行明细解析成 station_id。返回 (id|None, 原因)。

    原因码是给整改单和报告用的，必须能直接说清「缺什么」，不要写成「数据不合法」。
    """
    field = contract.get("field") or "station_id"
    alternatives = list(contract.get("alternatives") or [])
    sid = (row.get(field) or "").strip()
    if sid:
        if sid in maps["by_id"]:
            return sid, ""
        return None, f"station_id={sid} 不在 md_station 字典里"

    line_col, text_col = (alternatives + [None, None])[:2]
    line = (row.get(line_col) or "").strip() if line_col else ""
    text = (row.get(text_col) or "").strip() if text_col else ""
    if not line and not text:
        return None, f"既没有 {field}，也没有 {'/'.join(alternatives)}：无法唯一归属工位"

    # 工位文本直接就是主数据的业务码（产线A-01）：查字典，同时核对产线有没有说反。
    if text in maps.get("by_code", {}):
        row_master = maps["by_code"][text]
        sid = str(row_master["station_id"]).strip()
        got_line = (row_master.get("line_no") or "").strip()
        if line and line != got_line:
            return None, f"工位「{text}」属于 {got_line}，与提交的产线「{line}」矛盾"
        return sid, ""

    if not line:
        return None, f"工位文本是「{text}」但没有产线：同一工位号在四条产线上都成立"
    if not text:
        return None, f"有产线「{line}」但没有工位文本"
    if "工位" not in text:
        return None, f"工位文本「{text}」不是工位写法（既不是主数据业务码，也没有「工位」二字）"

    digits = _DIGITS.findall(text)
    if not digits:
        return None, f"工位文本「{text}」里没有工位号数字，只有产线名，不能当工位用"
    seq = int(digits[0])
    hits = maps["by_line_seq"].get((line, seq), [])
    if not hits:
        return None, f"({line}, 第{seq}工位) 在 md_station 里不存在"
    if len(hits) > 1:
        return None, f"({line}, 第{seq}工位) 匹配到 {len(hits)} 个 id：{', '.join(hits)}"
    return hits[0], ""


def audit(registry: dict, tables: dict[str, list[dict]], master_rows: list[dict], today: date | None = None) -> dict:
    """跑一遍：契约自身 → 每张明细的违约行 → 缺口认领状态。

    tables: {contract_id: [行]}，行里至少含 key_column、field、alternatives 两列。
    """
    today = today or date.today()
    problems = check_station_key(registry, master_rows)
    maps = station_maps(master_rows)

    out_contracts = []
    for c in contracts(registry):
        cid = c["id"]
        rows = tables.get(cid, [])
        keys = c.get("key_column") or "id"
        bad = []
        for r in rows:
            sid, why = resolve(r, c, maps)
            if sid is None:
                bad.append({"key": r.get(keys), "why": why})
        out_contracts.append(
            {
                "id": cid,
                "table": c.get("table"),
                "field": c.get("field"),
                "total": len(rows),
                "violations": len(bad),
                "samples": [b["key"] for b in bad[:5]],
                "keys": [str(b["key"]) for b in bad],
                "reasons": _reason_counts(bad),
                "owner": c.get("owner"),
                "due": c.get("due"),
                "details": bad[:10],
            }
        )
        if bad:
            problems.append(
                f"[{cid}] {c.get('table')} 违约 {len(bad)}/{len(rows)} 行（缺 {c.get('field')} 且无法按 "
                f"{'/'.join(c.get('alternatives') or [])} 查字典解析）；样例：{', '.join(str(b['key']) for b in bad[:3])}"
            )

    gaps = []
    for g in registry.get("field_gaps") or []:
        cid = g.get("contract")
        c = next((x for x in out_contracts if x["id"] == cid), None)
        if c is None:
            raise ContractNotRegistered(f"缺口 {g.get('id')!r} 指向未登记的契约 {cid!r}")
        violations = c["violations"]
        gap_problems = _gap_problems(g, violations, today)
        state = "closed" if violations == 0 else ("red" if gap_problems else "warn")
        if state == "red":
            problems.extend(f"[{g.get('id')}] {p}" for p in gap_problems)
        gaps.append(
            {
                "id": g.get("id"),
                "contract": cid,
                "state": state,
                "violations": violations,
                "owner": g.get("owner"),
                "due": g.get("due"),
                "note": _gap_note(g, violations, today),
            }
        )

    return {"problems": problems, "contracts": out_contracts, "gaps": gaps, "station_rows": len(master_rows)}


def _reason_counts(bad: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for b in bad:
        k = b["why"]
        k = "缺 station_id 与产线" if "既没有" in k else k
        k = "有工位文本但缺产线" if "没有产线" in k else k
        k = "文本里没有工位号" if "没有工位号数字" in k else k
        out[k] = out.get(k, 0) + 1
    return out


def _gap_problems(g: dict, violations: int, today: date) -> list[str]:
    problems = []
    if not (g.get("owner") or "").strip():
        problems.append(f"缺口没写责任方：{g.get('missing')}")
    due = g.get("due")
    if not due:
        problems.append(f"缺口没写到期日：{g.get('missing')}")
    else:
        d = date.fromisoformat(str(due))
        if d < today and violations > 0:
            problems.append(f"缺口已过期（到期 {due}，今天 {today.isoformat()}），仍有 {violations} 行违约")
    if not (g.get("missing") or "").strip():
        problems.append("缺口没写「缺什么」")
    return problems


def _gap_note(g: dict, violations: int, today: date) -> str:
    if violations == 0:
        return "已闭合：违约行归零"
    due = g.get("due")
    if not due:
        return f"仍有 {violations} 行违约，且没有到期日"
    left = (date.fromisoformat(str(due)) - today).days
    if left < 0:
        return f"仍有 {violations} 行违约，已过期 {abs(left)} 天"
    return f"认领中：{violations} 行违约，到期还有 {left} 天（{due}）"
