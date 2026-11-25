-- 增量水位表。注意它和 stg_* 是两回事：
--   stg_* 模拟客户数据，刻意不带约束；
--   这张表是我方元数据，可以有主键，因为它由我们写、只由我们读。

CREATE TABLE IF NOT EXISTS stg_watermark (
    table_name TEXT PRIMARY KEY,
    src        TEXT NOT NULL,
    mark       TIMESTAMP,
    rows_seen  INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

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

