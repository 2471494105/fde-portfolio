-- 增量水位表。注意它和 stg_* 是两回事：
--   stg_* 模拟客户数据，刻意不带约束；
--   这张表是我方元数据，可以有主键，因为它由我们写、只由我们读。

CREATE TABLE IF NOT EXISTS stg_watermark (
    table_name TEXT PRIMARY KEY,
    src        TEXT NOT NULL,
    mark       TIMESTAMP,
    mark_pk    TEXT,
    rows_seen  INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- 已存在的表补列（CREATE TABLE IF NOT EXISTS 不会改已有表结构）
ALTER TABLE stg_watermark ADD COLUMN IF NOT EXISTS mark_pk TEXT;

-- mark_pk 是复合水位的第二段。只存时间戳时，跟水位同一秒进来的行全部被判
-- 「已灌过」而漏掉；加上主键之后，(ts, pk) 严格大于水位才算新。
-- 代价：前提是源表主键单调递增。客户重传更小的主键（补录旧单）仍会漏，
-- 那需要重叠窗口 + 主键去重才能覆盖，属已知残留边界，不是这一列能解决的。

-- 隔离区：结构性不合格的行（比如时间在未来的）不往主表放，但要留证据。
-- 有主键 (src_table, row_id)，所以重复跑不会堆叠——这是它和主表最大的区别。
CREATE TABLE IF NOT EXISTS stg_quarantine (
    src_table TEXT NOT NULL,
    row_id    BIGINT NOT NULL,
    reason    TEXT NOT NULL,
    payload   JSONB NOT NULL,
    seen_at   TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (src_table, row_id)
);

