"""增量的决策部分：给定水位和一批数据，决定谁进主表、谁进隔离、水位推到哪。

只做决策，不碰数据库。这样边界能被单测钉住，脚本只负责把结果落库。

## 水位为什么是复合的 (时间戳, 主键)

第一版只存时间戳、用严格大于比较，结果同一秒进来的多条会被整体漏掉：
水位记到 T 之后，再来任何 ts == T 的行都不算新。设备在线时一秒几十条是常态，
这不是边角情况，是主路径。

改成 (ts, pk) 二元组后：跟水位同一时间戳、但主键更大的行算新数据，能进来。

## 四条前提（不满足就会安静地错，这几句比代码重要）

1. **主键在同一张源表内单调递增。** 复合比较只能识别「同时间戳里更大的主键」。
   客户如果重传一条更小的主键（补录旧单），它比水位小，会被判「已灌过」——
   这一步漏数据且不报错。**解法是给 `plan` 传 `overlap`**：把水位往回提一段时间
   再取候选，重叠区靠落库幂等去重（所以前提 2 必须先成立）。不给 `overlap` 时，
   这条边界原样保留。
2. **落库必须幂等。** plan 只按水位判断，不知道主表里已经有什么。客户重传整批、
   或水位是旧格式（只有时间戳、主键为空）时，plan 会把已灌过的行再吐一遍，
   由落库侧按主键去重（ON CONFLICT / 先查已有主键）兜住。
   plan 负责「谁该看一遍」，落库负责「谁是新的」，两个职责不搅在一起。
3. **未来时间既不进主表、也不参与水位。** 一条 2027 年的脏时间如果参与取 max，
   水位会跳到 2027，之后真实数据全被判「已灌过」，增量静默停摆。
4. **空批次不推进水位。** 宁可下次多判一次新，也不要把水位往前挪一格。
5. **水位只进不退。** 开了 `overlap` 之后，good 里可能全是重叠区里的旧行，
   直接取 max 会让新水位比原水位更早，下一轮又整段重捞。所以只有 max 严格
   大于原水位时才推进。
"""

from datetime import datetime, timedelta

# 主键为空时的排序键：比任何真实主键都小。这样「水位只记到时间戳」的旧格式
# 退化成「同一时间戳的行全部重看一遍」，靠落库幂等兜住，而不是直接漏掉。
_MIN_PK = (0, float("-inf"))


def _ts(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def _pk(v):
    """主键 -> 可比较的排序键。

    数字主键按数值比（否则 "10" < "9"），其余按字符串比。
    两种键加了类型前缀，混用时有确定的全序，不会抛 TypeError。
    """
    if v is None:
        return _MIN_PK
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, float(v))
    s = str(v).strip()
    try:
        return (0, float(s))
    except ValueError:
        return (1, s)


def _mark_key(mark):
    """外部水位 -> 可比较的 (ts, pk) 排序键；None 表示从未装载过。

    兼容两种入参：(datetime, pk) 二元组，或旧的纯 datetime。
    """
    if mark is None:
        return None
    if isinstance(mark, (tuple, list)):
        dt = mark[0]
        pk = mark[1] if len(mark) > 1 else None
    else:
        dt, pk = mark, None
    return (_ts(dt), _pk(pk))


def plan(rows, mark=None, now=None, ts_key="ts", pk_key="id", overlap=None):
    """返回 (进主表的行, 进隔离的行, 新水位)。

    新水位是 (时间戳, 主键原值) 二元组，可直接存库；主键缺失时为 (时间戳, None)。

    `overlap` 是 timedelta：给定时把水位往回提这么久取候选，且**只按时间比、
    不比主键**——因为要捞的正是「主键比水位小」的那一类。重叠区里的行已经
    在主表里，靠落库幂等去重（前提 2）。不给就是原来的严格复合比较。
    """
    now = now or datetime.now()
    mk = _mark_key(mark)

    def key(r):
        return (_ts(r[ts_key]), _pk(r.get(pk_key)) if pk_key else _MIN_PK)

    if mk is None:
        fresh = list(rows)
    elif overlap is not None and overlap > timedelta(0) and mk[0] is not None:
        floor = mk[0] - overlap
        fresh = [r for r in rows if _ts(r[ts_key]) >= floor]
    else:
        fresh = [r for r in rows if key(r) > mk]

    good = [r for r in fresh if _ts(r[ts_key]) <= now]
    bad = [r for r in fresh if _ts(r[ts_key]) > now]

    new_mark = mark
    if good:
        last = max(good, key=key)
        # 水位只进不退：重叠区里可能全是旧行，取 max 后仍早于原水位就不能推
        if mk is None or key(last) > mk:
            new_mark = (_ts(last[ts_key]), last.get(pk_key) if pk_key else None)
    return good, bad, new_mark
