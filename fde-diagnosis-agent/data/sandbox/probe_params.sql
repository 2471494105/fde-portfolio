-- B12 探针：验证「忘填 params 才红、本来没参数的不红」
--
-- 用途：改 B12 的判据之后，用它反向验一次——假红比漏报更贵，
-- 任何新断言必须先用干净数据/对照组验一遍。
--
-- 用法：
--   psql -U postgres -d fde_sandbox -f data\sandbox\probe_params.sql
--   python scripts\check_data_contract.py --dsn=postgresql://postgres:postgres@localhost:5432/fde_sandbox --only=B12
--   psql -U postgres -d fde_sandbox -c "TRUNCATE device_manual_chunks"
--
-- 预期结果：断言 1 条 PASS 0 / FAIL 1，坏行只有 T-BAD。
-- 注意：会 TRUNCATE 手册表，只能在 fde_sandbox 上跑，别在主库执行。

TRUNCATE device_manual_chunks;

INSERT INTO device_manual_chunks (chunk_uid, source, doc_type, section, content, params) VALUES
-- 正常块：有 params → 不在检查范围，不该红
('T-OK',    'test', '厂商手册', '1', '光源亮度设定为 80', '{"光源亮度设定": 80}'::jsonb),
-- 忘填块：正文明确提到参数名，params 却是空对象 → 必须红
('T-BAD',   'test', '厂内SOP',  '1', '光源亮度设定为 65，需与手册核对', '{}'::jsonb),
-- 对照块：告警码本来就没有可比对参数 → 不该红（这是不用 doc_type 白名单的理由）
('T-QUIET', 'test', '告警码表', '1', '告警码 E-101 相机未连接，可能是网线松脱', '{}'::jsonb);
