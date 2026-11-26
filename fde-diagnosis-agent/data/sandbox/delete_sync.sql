-- 删除/更新同步用的元数据表（我方表，可以有主键）
-- 纪律：客户数据落地表（stg_devices）刻意不加约束；删除状态放在这里，
-- 因为「谁被删了、为什么、几次缺席」是我们自己的判断，不是客户的数据。
--
-- 用法：psql -U postgres -d fde_sandbox -f data/sandbox/delete_sync.sql

CREATE TABLE IF NOT EXISTS stg_device_state (
    device_id     TEXT PRIMARY KEY,
    absent_count  INT NOT NULL DEFAULT 0,   -- 连续缺席次数（出现即归零）
    deleted_at    TIMESTAMP,                -- 非空 = 软删
    deleted_reason TEXT,                    -- event:<原因> / absent:<次数>
    protected     BOOLEAN NOT NULL DEFAULT false,  -- 有未闭环引用，不许删
    content_hash  TEXT,                     -- 上一轮内容指纹，用来判「改了没改」
    updated_at    TIMESTAMP NOT NULL DEFAULT now()
);

-- 审计表：每一次判定都留痕。软删是判断，判断要能被追问。
CREATE TABLE IF NOT EXISTS stg_delete_log (
    id          BIGSERIAL PRIMARY KEY,
    device_id   TEXT NOT NULL,
    action      TEXT NOT NULL,   -- mark_absent / soft_delete / revive / protect / noop
    reason      TEXT,
    absent_count INT,
    device_hash TEXT,            -- 判定时该设备的内容指纹
    decided_at  TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stg_delete_log_device_idx ON stg_delete_log (device_id, decided_at);
