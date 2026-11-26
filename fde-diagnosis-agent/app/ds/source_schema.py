"""源端 schema 登记：改字段的人不会通知你，得有人记着「上一版长什么样」。

为什么单独一层
--------------
我方那两条漂移判据（`CONTENT_COLS`、`FACT_KEYS`）读的都是**我们的表**。源端
在它自己的库里加一列，我们的 staging 永远不会出现那一列，判据也就永远看不见。
**判据再严，观测面错了就是零。**

所以这一层只做四件事：
  1. 登记：把「某客户某表，上次看到的列」写成 `data/mapping/source_schema/*.json`；
  2. 观测：从四种可靠信号读现状（CSV 表头 / Odoo `fields.*` / `information_schema` / 响应键）；
  3. 比对：加列、删列、改类型、改必填、改名，逐项列出来；
  4. 判破坏：主键/游标列消失、新增必填列 —— 这两类必须**拒跑**，不自动修。

依据（一手）：
  - Avro 解析规则：writer 有 reader 无 → 忽略（静默丢）；reader 有 writer 无且无默认 → 报错；
    rename 只认 `aliases`，且 spec 写的是 "may optionally" 被实现支持。
    https://avro.apache.org/docs/1.12.0/specification/
  - Confluent 兼容模式表：加**必填**列只有 FORWARD 允许；收窄标量只有 FORWARD 允许。
    https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html
  - Airbyte：游标/主键消失 = breaking，暂停等人工。
    https://docs.airbyte.com/platform/2.0/using-airbyte/schema-change-management

刻意不做的事：
  - 不做列名相似度匹配（"看着像就认"）。改名必须显式登记在 `renames` 里。
  - 不做自动并表（不把新列偷偷加进 CONTENT_COLS / FACT_KEYS）：历史指纹会因此不一致。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIR = ROOT / "data" / "mapping" / "source_schema"


class SourceSchemaNotRegistered(RuntimeError):
    """该客户该表没有源端登记。

    遇到它就停。没有登记就没有"上一版"，任何比对都会把"现状"当成"一直如此"——
    于是第一次发现变更永远是我们已经吃亏之后。
    """


def registry_path(client: str, table: str, root: Path | str | None = None) -> Path:
    base = Path(root) if root else REGISTRY_DIR
    return base / f"{client}_{table}.json"


def load(client: str, table: str, root: Path | str | None = None) -> dict:
    p = registry_path(client, table, root)
    if not p.exists():
        raise SourceSchemaNotRegistered(
            f"{client}/{table} 没有源端登记：缺 {p}。先跑一次观测并把结果写进登记，"
            "再谈「有没有变更」。"
        )
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- 观测：四种可靠信号 ----------


def _norm_col(spec) -> dict:
    """把 "col" 或 {"name": ..., ...} 统一成一个形状。"""
    if isinstance(spec, str):
        return {"name": spec, "type": None, "required": None}
    out = {"name": spec["name"], "type": spec.get("type"),
           "required": spec.get("required"), "unit": spec.get("unit")}
    if "renamed_from" in spec:
        out["renamed_from"] = spec["renamed_from"]
    return out


def observe_csv_header(path: Path | str) -> dict[str, dict]:
    """CSV 表头。按名读（`csv.DictReader`），所以列顺序变了不影响——但**没有表头**的文件不接。"""
    p = Path(path)
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        header = next(csv.reader(fh), None)
    if not header:
        raise ValueError(f"{p} 是空文件，没有表头")
    names = [h.strip() for h in header]
    if any(not n for n in names):
        raise ValueError(f"{p} 表头里有空列名（无表头的文件不要接）")
    return {n: {"name": n, "type": None, "required": None} for n in names}


_FIELD_RE = re.compile(
    r"^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*fields\.(?P<type>[A-Za-z]+)\("
    r"(?P<args>[^\n]*)",
    re.MULTILINE,
)


def parse_odoo_fields(text: str) -> dict[str, dict]:
    """Odoo 模型源码里的 `x = fields.Char(...)` 声明。

    这是「改字段的人会不会通知你」的第二种可靠信号：提交消息不可靠（G4 抓到的
    那次真实提交是删死代码），但模型声明变了就是字段变了。
    """
    out: dict[str, dict] = {}
    for m in _FIELD_RE.finditer(text):
        args = m.group("args") or ""
        required = "required=True" in args.replace(" ", "")
        out[m.group("name")] = {
            "name": m.group("name"),
            "type": m.group("type"),
            "required": required,
        }
    return out


def parse_information_schema(rows) -> dict[str, dict]:
    """`information_schema.columns` 的行（column_name, data_type, is_nullable）。"""
    out: dict[str, dict] = {}
    for r in rows:
        name, dtype, nullable = r[0], r[1], r[2]
        out[name] = {
            "name": name,
            "type": dtype,
            # information_schema 给的是 "IS_NULLABLE"（YES/NO），转成 required 语义
            "required": None if nullable is None else (str(nullable).upper() in ("NO", "N")),
        }
    return out


def parse_response_keys(payload) -> dict[str, dict]:
    """响应 JSON 的键集合（REST 类源）。

    只取第一层：嵌套对象当整体看，不递归猜结构——猜出来的 schema 比没有更危险。
    """
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise ValueError("响应不是对象或对象数组，取不到键集合")
    return {k: {"name": k, "type": None, "required": None} for k in payload}


# ---------- 比对 ----------


def diff(registry: dict, observed: dict[str, dict]) -> dict:
    """登记 vs 现状。返回可读的四类差异 + 未声明的改名嫌疑。

    `renames` 是登记里的 {"新名": "旧名"}：只有显式声明过的改名才算改名，
    其余「一加一减」一律按 suspects 报出来——Avro 的教训是 rename 会被当成
    add+drop 静默吞掉，我们不猜。
    """
    reg = {c["name"] if isinstance(c, str) else c["name"]: _norm_col(c)
           for c in registry.get("columns", [])}
    seen = dict(observed)
    renames = registry.get("renames") or {}

    renamed = []
    for new, old in renames.items():
        if new in seen and old not in seen:
            seen[old] = {**seen.pop(new), "renamed_from": new}
            renamed.append({"from": old, "to": new})

    added = sorted(n for n in seen if n not in reg)
    removed = sorted(n for n in reg if n not in seen)

    type_changed, type_unchecked = [], []
    required_changed = []
    for name in sorted(set(reg) & set(seen)):
        old, new = reg[name], seen[name]
        if old.get("type") and new.get("type"):
            if str(old["type"]).lower() != str(new["type"]).lower():
                type_changed.append({"column": name, "was": old["type"], "now": new["type"]})
        elif old.get("type") != new.get("type"):
            # CSV 表头给不出类型：这不是"没变"，是"这一维没观测到"
            type_unchecked.append(name)
        if old.get("required") is not None and new.get("required") is not None:
            if bool(old["required"]) != bool(new["required"]):
                required_changed.append(
                    {"column": name, "was": old["required"], "now": new["required"]})

    suspects = ({"added": added, "removed": removed}
                if added and removed else None)
    clean = not (added or removed or type_changed or required_changed)
    return {
        "added": added,
        "removed": removed,
        "type_changed": type_changed,
        "type_unchecked": type_unchecked,
        "required_changed": required_changed,
        "renamed": renamed,
        "rename_suspect": suspects,
        "clean": clean,
    }


def breaking(diffres: dict, registry: dict) -> list[str]:
    """必须拒跑的那几类。理由都是「不确定时不要删/不要改」，不是「做不到」。"""
    reasons = []
    keys = set(registry.get("key_columns") or [])
    hit = keys & set(diffres.get("removed") or [])
    if hit:
        reasons.append(f"主键/游标列消失：{sorted(hit)}（Airbyte 把这类定为 breaking，暂停等人工）")
    if diffres.get("rename_suspect"):
        s = diffres["rename_suspect"]
        if keys & (set(s["added"]) | set(s["removed"])):
            reasons.append(
                f"疑似改名的正是主键列：加 {s['added']} / 减 {s['removed']}，"
                "且没有在 renames 里声明")
    obs_added_required = [
        n for n in (diffres.get("added") or [])
        if (diffres.get("observed") or {}).get(n, {}).get("required") is True
    ]
    if obs_added_required:
        reasons.append(
            f"源端新增必填列：{obs_added_required}（历史行没有值；Confluent 表里只有 FORWARD 允许）")
    return reasons


def unconsumed(observed: dict[str, dict], consumed) -> list[str]:
    """源有、但我们没消费的列。**黄不是红**：这是「有意识忽略」的清单，逼人给个理由。"""
    used = set(consumed or [])
    return sorted(n for n in observed if n not in used)