"""增量的决策部分：给定水位和一批数据，决定谁进主表、谁进隔离、水位推到哪。

只做决策，不碰数据库。这样边界能被单测钉住，脚本只负责把结果落库。

三条规则：
1. 严格大于水位才算新数据。等于不算——这会漏掉同一秒进来的多条，
   是已知限制，不是 bug，见下面「为什么不做重叠」。
2. 时间在未来的行不进主表。它会给水位带来两种灾难：要么水位跳到未来、
   增量永久停摆；要么它永远大于水位、每晚被重复灌一次。两个坑的共同解是
   让它根本不参与主表比较。
3. 水位只由进主表的行决定。空批次不推进水位，宁可下次多判一次新，
   也不要把水位往前挪一格。
"""

from datetime import datetime


def _ts(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def plan(rows, mark=None, now=None, ts_key="ts"):
    """返回 (进主表的行, 进隔离的行, 新水位)。"""
    now = now or datetime.now()
    fresh = [r for r in rows if mark is None or _ts(r[ts_key]) > mark]
    good = [r for r in fresh if _ts(r[ts_key]) <= now]
    bad = [r for r in fresh if _ts(r[ts_key]) > now]
    new_mark = max((_ts(r[ts_key]) for r in good), default=mark)
    return good, bad, new_mark
