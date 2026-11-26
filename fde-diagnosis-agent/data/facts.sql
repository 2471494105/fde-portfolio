-- 事实层（L1）：设备状态的版本历史。目标库是 fde_rag（主库），不是 fde_sandbox。
--
-- 为什么不放沙箱：沙箱里的表每轮被检查脚本 TRUNCATE（那是为了「重复跑同一套断言」）。
-- 历史事实要留到三个月后，扔进沙箱就等于让「验证正确性」这个动作销毁证据。
--
-- 为什么单独 schema：多客户上线时第一刀切在 schema（tenant_a.facts / tenant_b.facts），
-- 表结构不用动。现在单客户也用同一个 schema，省得将来改表名。
--
-- 用法：psql -U postgres -d fde_rag -f data/facts.sql

CREATE SCHEMA IF NOT EXISTS facts;

CREATE TABLE IF NOT EXISTS facts.device_fact (
    id               BIGSERIAL PRIMARY KEY,
    tenant           TEXT NOT NULL,                  -- 客户标识；单客户时固定 client_a
    entity_id        TEXT NOT NULL,                  -- 设备号（= stg_devices.device_id）
    fact_key         TEXT NOT NULL,                  -- 属性名（= stg_devices 的列名）
    value            JSONB NOT NULL,                 -- 值本体；清空写 JSON null，不写 SQL NULL
    value_hash       TEXT NOT NULL,                  -- 值指纹：判「变没变」不比 JSON
    valid_from       TIMESTAMPTZ NOT NULL,           -- 这条事实从何时成立（口径见 valid_from_basis）
    valid_to         TIMESTAMPTZ,                    -- 非空 = 已作废；只作废，永不 DELETE
    valid_from_basis TEXT NOT NULL DEFAULT 'ingested'
                     CHECK (valid_from_basis IN ('ingested', 'effective')),
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    supersedes       BIGINT REFERENCES facts.device_fact(id),   -- 我取代了哪一行
    source           TEXT NOT NULL,
    CONSTRAINT valid_window CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- 核心约束：同一个键，同一时刻只能有一条「当前」。
-- 部分索引（WHERE valid_to IS NULL）让历史行不占位，「改过多少次」不受限制；
-- 而「两条同时有效」是数据错误，必须由数据库拒绝——代码写错时这是最后一道防线。
--
-- 名字不能叫 device_fact_current：索引和视图在 PG 里共用同一个 schema 命名空间，
-- 同名会直接报 "不是一个视图"。
CREATE UNIQUE INDEX IF NOT EXISTS device_fact_unique_current
    ON facts.device_fact (tenant, entity_id, fact_key)
    WHERE valid_to IS NULL;

-- 关闭原因：改值（superseded）与设备被删（deleted:<reason>）必须能分开。
-- 不加这一列，「这台设备的历史为什么停在某一天」只能靠猜——而 L1 存在的意义就是不用猜。
-- 用 ADD COLUMN IF NOT EXISTS 而不是改上面的 CREATE：表已经建过，改 CREATE 不会生效。
ALTER TABLE facts.device_fact ADD COLUMN IF NOT EXISTS close_reason TEXT;

-- 历史查询：某台设备某个属性的时间线
CREATE INDEX IF NOT EXISTS device_fact_history
    ON facts.device_fact (tenant, entity_id, fact_key, valid_from DESC);

-- 当前值视图：调用方只想知道「现在是什么」，不该各自去写 WHERE valid_to IS NULL
CREATE OR REPLACE VIEW facts.device_fact_current AS
    SELECT id, tenant, entity_id, fact_key, value, value_hash,
           valid_from, valid_from_basis, ingested_at, supersedes, source
      FROM facts.device_fact
     WHERE valid_to IS NULL;

COMMENT ON TABLE  facts.device_fact IS '设备事实的版本历史：只作废不删，一个键同时只有一条当前值';
COMMENT ON COLUMN facts.device_fact.valid_from_basis IS
    'valid_from 的口径：ingested=到货时刻（当前唯一能做到的），effective=上游明确给的生效时刻';
COMMENT ON COLUMN facts.device_fact.supersedes IS
    '这一行取代了哪一行；链条断了就没有「从 A 改到 B」可讲，只剩一堆孤立的值';
