"""源端值域抽查（S5）：schema 一个字都没变，但值的意思变了。

S1 看的是「列还在不在」，看不见这些：
  - 百分制改成小数（90 → 0.9）：列名、类型都一样，我们的 `scale` 照乘 0.01 → 0.009
  - 枚举多出一个值：词表没有它 → 被机械归进某一类
  - 日期格式换了一版：`2025/03/12` → `2025-03-12`，解析器按老格式全失败或全歪
  - 必填列开始给空：空值一路放行到断言层，最后表现为「结论说不清」

这一层靠**样本值**判，不靠声明。声明（`domains`）写在源端登记里，和列名同一份文件。

分三档，理由不同：
  breaking  量纲跳变 / 全部不可解析 —— 基准变了，符号乘错就是「所有结论反向」，拒跑
  red       超范围 / 部分不可解析 —— 可能是新数据边界，先看，人来决定
  yellow    枚举扩值 / 空值率 —— 多数是新业务状态，先看清单；状态词表由卡 1 的 B 判据管
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BREAKING, RED, YELLOW, NOTE = "breaking", "red", "yellow", "note"

DATE_CANDIDATES = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
                   "%Y-%m-%dT%H:%M:%S")

DEFAULT_LIMIT = 200


def load_domains(registry: dict) -> dict:
    return registry.get("domains") or {}


def observe_values(path, limit: int | None = DEFAULT_LIMIT) -> tuple[list, int]:
    """读样本值。返回 (行, 实际读了多少行)；大文件默认只抽前 200 行并在输出里说清楚。"""
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader):
            if limit and i >= limit:
                break
            rows.append(row)
    return rows, len(rows)


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _finding(col, check, severity, message, samples=None) -> dict:
    return {"column": col, "check": check, "severity": severity,
            "message": message, "samples": samples or []}


def check_range(col: str, values: list, dom: dict) -> list:
    """只有声明了 range 才比数值。声明了 format 或 enum 的列不该走这里——它们是文本。"""
    if not dom.get("range"):
        return []
    lo, hi = dom["range"]
    sev = dom.get("range_severity", RED)
    filled = [v for v in values if str(v).strip() != ""]
    if not filled:
        return []
    unparsed = [v for v in filled if _num(v) is None]
    out = []
    if unparsed:
        out.append(_finding(col, "range", sev,
                            f"有 {len(unparsed)}/{len(filled)} 个值不是数值", unparsed[:3]))
    nums = [n for n in (_num(v) for v in filled) if n is not None]
    if not nums:
        return out
    mn, mx = min(nums), max(nums)
    if hi and lo is not None and hi > 0:
        # 百分制→小数：声明 [0,100]，观测 max=0.91
        if 0 <= mx <= hi / 100:
            out.append(_finding(col, "range", BREAKING,
                                f"量纲疑似缩小 100 倍（百分制→小数）：声明 [{lo},{hi}]，观测 "
                                f"min={mn} max={mx}", [mn, mx]))
            return out
        if mn >= hi:
            if mn >= hi * 10:
                out.append(_finding(col, "range", BREAKING,
                                    f"量纲疑似放大 100 倍：声明 [{lo},{hi}]，观测 min={mn} "
                                    f"max={mx}", [mn, mx]))
                return out
        far = [n for n in nums if n < lo or n > hi]
        if far:
            out.append(_finding(col, "range", sev,
                                f"{len(far)}/{len(nums)} 个值超出声明 [{lo},{hi}]",
                                far[:3]))
    return out


def check_enum(col: str, values: list, dom: dict) -> list:
    allowed = {str(x) for x in (dom.get("enum") or [])}
    if not allowed:
        return []
    seen = [str(v).strip() for v in values if str(v).strip() != ""]
    new = sorted({v for v in seen if v not in allowed})
    if not new:
        return []
    return [_finding(col, "enum", YELLOW,
                     f"出现词表外的值 {len(new)} 个（声明 {sorted(allowed)}）", new[:5])]


def check_format(col: str, values: list, dom: dict) -> list:
    fmt = dom.get("format")
    if not fmt:
        return []
    filled = [str(v).strip() for v in values if str(v).strip() != ""]
    if not filled:
        return []
    bad = []
    for v in filled:
        try:
            datetime.strptime(v, fmt)
        except ValueError:
            bad.append(v)
    if not bad:
        return []
    if len(bad) == len(filled):
        for alt in DATE_CANDIDATES:
            if alt == fmt:
                continue
            if all(_parses(v, alt) for v in filled):
                return [_finding(col, "format", BREAKING,
                                 f"日期格式整体改了：声明 {fmt}，实际全都能按 {alt} 解析",
                                 filled[:2])]
        return [_finding(col, "format", BREAKING,
                         f"全部 {len(filled)} 个值都解析不了（声明 {fmt}）", filled[:2])]
    return [_finding(col, "format", RED,
                     f"{len(bad)}/{len(filled)} 个值不符合 {fmt}", bad[:3])]


def _parses(v: str, fmt: str) -> bool:
    try:
        datetime.strptime(v, fmt)
        return True
    except ValueError:
        return False


def check_empty(col: str, values: list, dom: dict) -> list:
    empty = sum(1 for v in values if str(v).strip() == "")
    if not values or not empty:
        return []
    rate = empty / len(values)
    if dom.get("allow_empty"):
        return [_finding(col, "empty", NOTE,
                         f"空值 {empty}/{len(values)}（声明允许空）", [])]
    return [_finding(col, "empty", YELLOW,
                     f"空值 {empty}/{len(values)}（声明不允许空）", [])]


def analyze(client: str, table: str, path=None, limit: int | None = DEFAULT_LIMIT) -> dict:
    from app.ds import source_schema as ss

    reg = ss.load(client, table)
    doms = load_domains(reg)
    src = Path(path) if path else ss.ROOT / reg["source_path"]
    rows, scanned = observe_values(src, limit)
    findings = []
    for col, dom in doms.items():
        if col not in (rows[0] if rows else {}):
            findings.append(_finding(col, "missing", RED, "样本里没有这一列", []))
            continue
        values = [r.get(col, "") for r in rows]
        findings += check_range(col, values, dom)
        findings += check_enum(col, values, dom)
        findings += check_format(col, values, dom)
        findings += check_empty(col, values, dom)

    severities = {f["severity"] for f in findings}
    exit_code = 3 if BREAKING in severities else (1 if RED in severities else 0)
    return {
        "client": client,
        "table": table,
        "source": str(src),
        "rows_scanned": scanned,
        "domains_checked": sorted(doms),
        "findings": findings,
        "status": {3: "breaking", 1: "red", 0: "ok" if not findings else "warn"}[exit_code],
        "exit": exit_code,
    }