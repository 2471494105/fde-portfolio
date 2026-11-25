-- 跨源测试用的落地表（staging）：刻意**不带任何约束**
-- 目的：模拟「CSV / 外部库 / 客户自有系统」——那边没有 PK、没有 FK、没有 CHECK。
-- 断言在 pg 上永远绿（DB 挡着），在这里才会真的红，这正是要测的东西。
--
-- 用法：psql -U postgres -d fde_sandbox -f data/sandbox/staging.sql

CREATE TABLE IF NOT EXISTS stg_devices (
    device_id              TEXT,
    line                   TEXT,
    station                TEXT,
    camera_model           TEXT,
    lens_model             TEXT,
    light_model            TEXT,
    install_date           DATE,
    maintenance_cycle_days INT,
    _src                   TEXT NOT NULL,
    _loaded_at             TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stg_defect_logs (
    id             BIGINT,
    ts             TIMESTAMP,
    device_id      TEXT,
    station        TEXT,
    defect_type    TEXT,
    confidence     REAL,
    batch          TEXT,
    verdict        TEXT,
    is_actually_ok BOOLEAN,
    _src           TEXT NOT NULL,
    _loaded_at     TIMESTAMP NOT NULL DEFAULT now()
);
