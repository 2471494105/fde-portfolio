"""字段映射：客户叫 equip_no，我们叫 device_id，中间翻译一次。

为什么需要：换源时最常撞的不是「没数据」，是「字段名不一样、日期格式不一样、
单位不一样」。没有这层，每接一家客户就要改一次 loader，改一次就要重跑。

三条刻意的设计：
1. **映射是显式承诺**。配置文件里写明的字段才是我们认的字段，源文件里多出来的
   列一律忽略，不做列名模糊匹配、不做自动探测——猜错比报错贵。
2. **转换失败要报，不许静默变空**。一行里日期写了 2025/13/45，不能让这条记录
   悄悄变成 NULL 混进库（那正是「干净数据」报表在骗人的成因），要带着行号和
   原值一起抛出来。
3. **缺列和空值分开**。源里根本没有 equip_no 这一列 = 配置过时或文件变了，必须报；
   列在、值是空的 = 数据脏，原样放行，交给断言层（B1 抓的就是这个）。
   映射层只负责翻译，不负责判对错——判对错是断言层的职责，两边各管一段。
"""

import json
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_TRUE = {"1", "t", "true", "y", "yes", "是", "ok", "pass", "合格"}
_FALSE = {"0", "f", "false", "n", "no", "否", "ng", "fail", "不合格"}


class MappingError(ValueError):
    """映射配置或数据转换出错。"""


def _conv_text(v):
    return str(v).strip()


def _conv_int(v):
    return int(str(v).replace(",", "").strip())


def _conv_real(v):
    return float(str(v).replace(",", "").strip())


def _conv_bool(v):
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"无法判断真假：{v!r}")


def _conv_date(v, fmt=None):
    s = str(v).strip()
    if fmt:
        return datetime.strptime(s, fmt).date()
    return date.fromisoformat(s[:10])


def _conv_timestamp(v, fmt=None):
    s = str(v).strip()
    if fmt:
        return datetime.strptime(s, fmt)
    return datetime.fromisoformat(s)


_CONVERTERS = {
    "text": _conv_text,
    "int": _conv_int,
    "real": _conv_real,
    "bool": _conv_bool,
    "date": _conv_date,
    "timestamp": _conv_timestamp,
}


class Mapping:
    """一份客户侧字段到内部字段的翻译表。"""

    def __init__(self, spec: dict):
        self.name = spec.get("name", "unnamed")
        self.tables = spec.get("tables", {})

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def source_name(self, table: str) -> str:
        return self.tables[table].get("source") or f"{table}.csv"

    def target_fields(self, table: str) -> list:
        return list(self.tables[table]["fields"])

    def apply(self, table: str, raw_rows) -> list:
        """把客户行翻成内部行。有转换错误就整体抛出，不返回半成品。"""
        if table not in self.tables:
            raise MappingError(f"映射里没有 {table} 这张表")
        spec = self.tables[table]["fields"]
        out, errors = [], []

        for lineno, raw in enumerate(raw_rows, start=1):
            rec = {}
            for target, cfg in spec.items():
                src = cfg.get("from", target)
                if src not in raw:
                    if cfg.get("optional"):
                        rec[target] = cfg.get("default")
                        continue
                    errors.append(f"第{lineno}行缺少列 {src!r}，实际列：{sorted(raw)}")
                    continue

                val = raw.get(src)
                if val is None or (isinstance(val, str) and not val.strip()):
                    if "default" in cfg:
                        rec[target] = cfg["default"]
                    elif cfg.get("not_null"):
                        errors.append(f"第{lineno}行 {src} 为空（该列不允许空）")
                    else:
                        # 空值是数据脏，不是配置错：原样放行，让断言去抓
                        rec[target] = "" if isinstance(val, str) else None
                    continue

                try:
                    rec[target] = self._convert(val, cfg)
                except Exception as exc:
                    typ = cfg.get("type", "text")
                    errors.append(f"第{lineno}行 {src}={val!r} 转 {typ} 失败：{exc}")
            out.append(rec)

        if errors:
            head = "；".join(errors[:5])
            more = "" if len(errors) <= 5 else f"（另有 {len(errors) - 5} 处）"
            raise MappingError(f"{len(errors)} 处转换失败：{head}{more}")
        return out

    @staticmethod
    def _convert(val, cfg):
        typ = cfg.get("type", "text")
        conv = _CONVERTERS.get(typ)
        if conv is None:
            raise MappingError(f"未知类型 {typ!r}")
        if typ in ("date", "timestamp"):
            return conv(val, cfg.get("format"))
        num = conv(val)
        if typ in ("int", "real") and cfg.get("scale") is not None:
            return num * cfg["scale"]
        return num
