-- 视觉检测设备故障诊断 Agent · 主库 schema
-- 目标库：fde_rag（复用项目一的 PostgreSQL 18.6 + pgvector，手册表 doc_chunks 不重建）
-- 说明：本 schema 只新增表，不修改项目一已有表。

BEGIN;

-- ---------- 设备档案 ----------
CREATE TABLE IF NOT EXISTS devices (
    device_id              TEXT PRIMARY KEY,
    line                   TEXT NOT NULL,
    station                TEXT NOT NULL,
    camera_model           TEXT NOT NULL,
    lens_model             TEXT NOT NULL,
    light_model            TEXT NOT NULL,
    install_date           DATE NOT NULL,
    maintenance_cycle_days INT  NOT NULL
);

COMMENT ON TABLE devices IS '视觉检测设备档案，5 台';

-- ---------- 误判日志（主证据表） ----------
CREATE TABLE IF NOT EXISTS defect_logs (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMP NOT NULL,
    device_id      TEXT NOT NULL REFERENCES devices(device_id),
    station        TEXT NOT NULL,
    defect_type    TEXT NOT NULL,
    confidence     REAL NOT NULL,
    batch          TEXT NOT NULL,
    verdict        TEXT NOT NULL CHECK (verdict IN ('OK', 'NG')),
    is_actually_ok BOOLEAN NOT NULL
);

COMMENT ON TABLE defect_logs IS '每次判定记录；verdict=模型判定，is_actually_ok=人工复判结果';
COMMENT ON COLUMN defect_logs.confidence IS '模型置信度 0-1';

CREATE INDEX IF NOT EXISTS idx_defect_logs_dev_ts ON defect_logs (device_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_defect_logs_station  ON defect_logs (device_id, station);
CREATE INDEX IF NOT EXISTS idx_defect_logs_batch    ON defect_logs (batch);
CREATE INDEX IF NOT EXISTS idx_defect_logs_type     ON defect_logs (device_id, defect_type);

-- ---------- 告警码字典 ----------
CREATE TABLE IF NOT EXISTS alarms (
    code         TEXT PRIMARY KEY,
    meaning      TEXT NOT NULL,
    common_cause TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical'))
);

COMMENT ON TABLE alarms IS '设备自诊断告警码字典';

-- ---------- 告警发生记录 ----------
CREATE TABLE IF NOT EXISTS alarm_events (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMP NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    code      TEXT NOT NULL REFERENCES alarms(code)
);

CREATE INDEX IF NOT EXISTS idx_alarm_events_dev_ts ON alarm_events (device_id, ts DESC);

-- ---------- 标定记录 ----------
CREATE TABLE IF NOT EXISTS calibrations (
    id        BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    ts        TIMESTAMP NOT NULL,
    drift_px  REAL NOT NULL,
    drift_mm  REAL NOT NULL,
    result    TEXT NOT NULL CHECK (result IN ('通过', '告警', '失败')),
    operator  TEXT NOT NULL
);

COMMENT ON COLUMN calibrations.drift_px IS '像素级标定漂移';
COMMENT ON COLUMN calibrations.drift_mm IS '换算到物方的漂移量（mm）';

CREATE INDEX IF NOT EXISTS idx_calibrations_dev_ts ON calibrations (device_id, ts DESC);

-- ---------- 光源使用 ----------
CREATE TABLE IF NOT EXISTS light_usage (
    device_id        TEXT PRIMARY KEY REFERENCES devices(device_id),
    light_model      TEXT NOT NULL,
    cumulative_hours INT  NOT NULL,
    rated_life_hours INT  NOT NULL,
    brightness_set   INT  NOT NULL,
    last_replaced_at DATE
);

COMMENT ON COLUMN light_usage.rated_life_hours IS '厂商标称寿命（小时），如 CA-DC21E 配套光源按 8000h';

CREATE TABLE IF NOT EXISTS light_brightness_history (
    id        BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    ts        TIMESTAMP NOT NULL,
    value     INT NOT NULL
);

COMMENT ON TABLE light_brightness_history IS '亮度设定变更历史（谁把 80 调到 65）';

-- ---------- 工单（由 mock QMS 读写） ----------
CREATE TABLE IF NOT EXISTS work_orders (
    order_id        TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    root_cause_code TEXT NOT NULL DEFAULT 'other',
    severity        TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    actions         TEXT NOT NULL,
    evidence_refs   TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status          TEXT NOT NULL,
    assignee_group  TEXT NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

COMMENT ON COLUMN work_orders.idempotency_key IS 'hash(device_id + root_cause_code)，**不含日期**：跨天的重复建单也要挡';

-- 已有表的结构变迁（CREATE TABLE IF NOT EXISTS 不会改旧表）。
-- 顺序有讲究：旧的"全表唯一"必须**先拆**，否则新约束建了也没用——
-- 旧约束还在，已完成过的键依然占位，同一问题处理完再坏永远建不了新单。
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS root_cause_code TEXT NOT NULL DEFAULT 'other';
ALTER TABLE work_orders DROP CONSTRAINT IF EXISTS work_orders_idempotency_key_key;

-- 幂等的正确约束是"同一件事同时只有一张未关闭的单"，不是"同一个键全表只能有一条"。
-- 用部分唯一索引表达：已关闭的单不占位，同一问题处理完之后再坏，还能建新单。
-- 条件用**黑名单**（排除已关闭状态）而不是白名单（只收待处理）：
-- 将来新增任何状态，默认仍算"还挂着"、仍参与去重——宁可多挡，不会漏挡。
-- 早先这么写：
--   idempotency_key TEXT UNIQUE + 键里带 day
-- 结果是"每天的同一件事各建一张"——CX-101 一个坏光源连着三天三张待处理单，
-- 设备科不知道该处理哪张，而数据库认为这三张都合法。
CREATE UNIQUE INDEX IF NOT EXISTS work_orders_open_key
    ON work_orders (idempotency_key)
    WHERE status NOT IN ('已完成', '已合并', '已取消');

-- ---------- 设备手册片段（与项目一 IQC 文档库分离） ----------
-- 项目一 doc_chunks 装的是 IQC 检验标准/安全规范，不含设备参数，
-- 所以设备诊断手册单独建表，避免污染已交付的知识库。
CREATE TABLE IF NOT EXISTS device_manual_chunks (
    chunk_uid  TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    doc_type   TEXT,
    section    TEXT,
    content    TEXT NOT NULL,
    params     JSONB,
    embedding  vector(768),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

COMMENT ON TABLE device_manual_chunks IS '视觉检测设备手册/作业指导书片段，含人工植入的参数冲突';

-- 已存在的表补列（CREATE TABLE IF NOT EXISTS 不会修改已有表结构）
ALTER TABLE device_manual_chunks ADD COLUMN IF NOT EXISTS params JSONB;

CREATE INDEX IF NOT EXISTS idx_dmc_embedding
    ON device_manual_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_dmc_source ON device_manual_chunks (source);

COMMIT;
