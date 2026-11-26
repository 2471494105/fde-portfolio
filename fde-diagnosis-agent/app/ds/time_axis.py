"""连接层的时间轴口径：源端按哪个字段筛、本地记哪个字段、业务「发生时刻」是哪个。

为什么单列一份：这三件事在代码里本来就分在三处——源端查询的 `since`、`plan()` 的
`ts_key`、`stg_watermark` 的 `mark`。只要有一处写错，增量就会「静默丢」而不是报错。

## K-3 的实证（外部样本，2026-11-26）

GitHub `/repos/{owner}/{repo}/commits?since=T` 按 **committer date** 过滤，返回里
`author.date` 可以早到 2025-12-15（请求 `since=2026-11-20` 时实测）。本地水位如果记的
是 author 时间，这批「author 早于水位」的提交下一轮会被判「已灌过」→ 整条静默丢弃，
没有任何报错。

所以判据不是「三个名字一样」，而是「三个钟一样」——**名字一样、语义不同的两个 ts
更危险**，因为它们看起来一致，机器也查不出，只能靠显式登记 + 实跑探针。

## 七条规则（axis_issues 实现，全部可机器判定）

1. 三个字段必须显式登记，空值即红
2. 本地水位字段必须等于业务时间字段：水位记的不是发生时刻，补录单会被判已灌过
3. 三个字段都不许落在「入库时钟」黑名单：`_loaded_at` / `created_at` / `ingested_at` …
   到货时间不是发生时刻，用它做水位等于把「晚到」当成「没有」
4. `filter_clock` 只能是三个枚举值之一（same_clock / later_than_business /
   earlier_than_business）
5. 源端过滤字段与业务时间字段不同名，却登记成 same_clock → 红（先证明钟相同）
6. 钟不同时（later / earlier）必须写清丢了什么（`drop_scenario` 非空）
7. 给了表列清单时，两个本地字段必须真的存在——登记的名字在表里没有，就是一句空话

规则 2~7 的共同点：**它们都不改变任何行为，只让「口径不一致」从静默变成红**。
"""

# 只接受这三种关系。没有「不确定」——不确定就该去核实，而不是登记一个模糊值。
FILTER_CLOCKS = ("same_clock", "later_than_business", "earlier_than_business")

# 入库时钟黑名单：这些列回答「我们什么时候拿到」，不回答「它什么时候发生」。
# 用它们做水位，晚到的补录单会被判已灌过——正是 K-3 那种静默丢。
INGEST_CLOCK_DENY = (
    "_loaded_at",
    "_src",
    "created_at",
    "ingested_at",
    "etl_time",
    "load_time",
    "inserted_at",
    "arrived_at",
    "received_at",
)

# 每张走增量的源表登记一条。新增源表必须同时补这里，否则 data_health 会红。
TIME_AXIS = {
    "defect_logs": {
        "source_filter_field": "ts",
        "local_watermark_field": "ts",
        "business_time_field": "ts",
        "filter_clock": "same_clock",
        "drop_scenario": "",
        "evidence": (
            "K-3 实证（2026-11-26）：GitHub commits 的 since 按 committer date 过滤，"
            "返回里 author.date 可早到 2025-12-15。本地若记 author 时间，"
            "这批行会被判已灌过而整条静默丢。本项目源端与业务时间是同一列 ts，"
            "但这一点是「当前假设」，不是外部保证——接真源时必须重核一次。"
        ),
    },
}


def _norm(name):
    return (name or "").strip().lower()


def axis_issues(axis, table_cols=None):
    """返回口径问题列表，空列表才算通过。

    `axis` 是 TIME_AXIS 里的一条登记；`table_cols` 给定时额外核对字段真伪。
    纯函数，不连库——所以单测能直接钉住每一条规则。
    """
    if not axis:
        return ["时间轴没登记（源端过滤 / 本地水位 / 业务时间三个字段一个都没写）"]

    out = []
    for key in ("source_filter_field", "local_watermark_field", "business_time_field"):
        if not _norm(axis.get(key)):
            out.append(f"时间轴登记缺 {key}")

    src = _norm(axis.get("source_filter_field"))
    mark = _norm(axis.get("local_watermark_field"))
    biz = _norm(axis.get("business_time_field"))

    for key, name in (
        ("source_filter_field", src),
        ("local_watermark_field", mark),
        ("business_time_field", biz),
    ):
        if name and name in INGEST_CLOCK_DENY:
            out.append(
                f"{key} 登记成了入库时钟 {name}："
                "到货时间不是发生时刻，拿它做水位会让晚到的补录单静默丢"
            )

    if mark and biz and mark != biz:
        out.append(
            f"本地水位记 {mark}、业务时间字段是 {biz}："
            "水位不是发生时刻，晚到的补录单会被判已灌过"
        )

    clock = (axis.get("filter_clock") or "").strip()
    if clock not in FILTER_CLOCKS:
        out.append(
            f"filter_clock 未登记或非法：{clock or '（空）'}"
            f"（只接受 {'/'.join(FILTER_CLOCKS)}）"
        )
    elif src and biz and src != biz and clock == "same_clock":
        out.append(
            f"源端按 {src} 过滤、业务时间是 {biz}，却登记成同一时钟："
            "名字不同就得先证明钟相同，否则整批可能静默丢"
        )
    elif clock != "same_clock" and not (axis.get("drop_scenario") or "").strip():
        out.append(
            f"源端过滤与业务时间不同钟（{clock}），但没写丢了什么"
            "（drop_scenario 为空：说不清丢什么，就等于承认会丢）"
        )

    if table_cols:
        cols = {_norm(c) for c in table_cols}
        for key, name in (
            ("local_watermark_field", mark),
            ("business_time_field", biz),
        ):
            if name and name not in cols:
                out.append(
                    f"{key} 登记的 {name} 在表里不存在"
                    f"（实际列：{', '.join(sorted(cols)) if cols else '空'}）"
                )
    return out
