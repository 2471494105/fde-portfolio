-- 沙箱脏数据：模拟客户现场「能突破 DB 约束」的坏行
-- 用途：验证数据断言到底该查什么——凡是 DB 自己会拒绝的，断言不必重复
-- 用法：psql -U postgres -d fde_sandbox -f data/sandbox/dirty_seed.sql
-- 目标库只能是 fde_sandbox，永远不要指向 fde_rag
--
-- 首次使用（重建沙箱，四步）：
--   psql -U postgres -c "CREATE DATABASE fde_sandbox"
--   psql -U postgres -d fde_sandbox -c "CREATE EXTENSION IF NOT EXISTS vector"
--   psql -U postgres -d fde_sandbox -f data/schema.sql
--   psql -U postgres -d fde_sandbox -f data/sandbox/dirty_seed.sql

SET client_encoding = 'UTF8';

BEGIN;

TRUNCATE devices, defect_logs, alarms, alarm_events, calibrations,
         light_usage, light_brightness_history, work_orders
    RESTART IDENTITY CASCADE;

-- ---------- devices ----------
INSERT INTO devices (device_id, line, station, camera_model, lens_model, light_model, install_date, maintenance_cycle_days) VALUES
 ('CX-101', '3号线', '1-3号工位', 'In-Sight 2801', '16mm', 'CA-DC21E', '2025-03-12', 90),
 ('CX-102', '3号线', '1-3号工位', 'In-Sight 2802', '16mm', 'CA-DC21E', '2025-05-20', 90),
 ('CX-104', '4号线', '1-2号工位', 'CV-X450 + CA-H200CX', '16mm', 'CA-DC21E', '2024-08-15', 90),
 -- [D1] 大小写重复：PK 允许，业务上这是 CX-101 的第二份档案
 ('cx-101', '3号线', '1-3号工位', 'In-Sight 2801', '16mm', 'CA-DC21E', '2025-03-12', 90),
 -- [D13] 设备号是空字符串：NOT NULL 只拦 NULL，空串照样进，PK 也允许
 ('', '3号线', '1-3号工位', 'In-Sight 2801', '16mm', 'CA-DC21E', '2025-03-12', 90);

-- ---------- defect_logs ----------
INSERT INTO defect_logs (ts, device_id, station, defect_type, confidence, batch, verdict, is_actually_ok) VALUES
 ('2026-11-01 08:00:00', 'CX-101', '2号工位', '划伤', 0.91, 'B20261101', 'NG', false),
 ('2026-11-01 09:00:00', 'CX-101', '2号工位', '划伤', 0.88, 'B20261101', 'NG', false),
 ('2026-11-01 10:00:00', 'CX-102', '2号工位', '污点', 0.75, 'B20261101', 'OK', true),
 -- [D2] 时间在未来（客户系统时钟错 / 手工补录写错年份）
 ('2027-01-01 08:00:00', 'CX-101', '2号工位', '划伤', 0.90, 'B20261101', 'NG', false),
 -- [D3] 置信度 1.5（不是 0-1）
 ('2026-11-02 08:00:00', 'CX-101', '2号工位', '划伤', 1.5, 'B20261102', 'NG', false),
 -- [D4] 置信度 -0.1
 ('2026-11-02 09:00:00', 'CX-102', '2号工位', '污点', -0.1, 'B20261102', 'OK', true),
 -- [D5] 工位与档案不一致：CX-101 在 3号线 1-3号工位
 ('2026-11-02 10:00:00', 'CX-101', '4号线', '划伤', 0.92, 'B20261102', 'NG', false),
 -- [D6] 空批次号（NOT NULL 拦不住空串）
 ('2026-11-03 08:00:00', 'CX-102', '2号工位', '污点', 0.80, '', 'OK', true),
 -- [D7] 批次号只有 1 个字符
 ('2026-11-03 09:00:00', 'CX-102', '2号工位', '污点', 0.81, 'A', 'OK', true);

-- ---------- alarms ----------
INSERT INTO alarms (code, meaning, common_cause, severity) VALUES
 ('E1001', '光源亮度不足', '光源老化或设定被调低', 'warning'),
 ('E1002', '相机触发超时', 'PLC 与相机时序不同步', 'critical'),
 ('E1003', '镜头污染', '镜头附着粉尘', 'info');

-- ---------- alarm_events ----------
INSERT INTO alarm_events (ts, device_id, code) VALUES
 ('2026-11-01 08:05:00', 'CX-101', 'E1001'),
 ('2026-11-01 09:10:00', 'CX-102', 'E1003'),
 -- [D8] 时间在未来
 ('2030-01-01 00:00:00', 'CX-104', 'E1002');

-- ---------- calibrations ----------
INSERT INTO calibrations (device_id, ts, drift_px, drift_mm, result, operator) VALUES
 ('CX-101', '2026-10-20 14:00:00', 0.12, 0.07, '通过', '张伟'),
 -- [D9] 像素漂移与物方漂移符号相反：一正一负说明换算公式或坐标方向有问题
 ('CX-102', '2026-10-21 14:00:00', 0.18, -0.11, '告警', '李娜');

-- ---------- light_usage ----------
INSERT INTO light_usage (device_id, light_model, cumulative_hours, rated_life_hours, brightness_set, last_replaced_at) VALUES
 ('CX-101', 'CA-DC21E', 1200, 8000, 78, '2025-03-12'),
 -- [D10] 累计时长是标称寿命的 4.6 倍：大概率单位错（分钟记成小时）
 ('CX-102', 'CA-DC21E', 9200, 2000, 65, '2025-05-20');

-- ---------- light_brightness_history ----------
-- [D11] 故意留空表：空表最会骗人——查询返回空，看起来像「没有异常」
--       本项目 I9 六轮全红就是这种：库里没单，不是逻辑错

-- ---------- work_orders ----------
INSERT INTO work_orders (order_id, device_id, root_cause, root_cause_code, severity, actions, evidence_refs, idempotency_key, status, assignee_group) VALUES
 ('WO-SB-001', 'CX-101', '光源亮度不足', 'light', 'medium', '更换光源', '[]', 'k-101-light', '待处理', '设备科'),
 -- [D12] device_id 不存在：work_orders.device_id 没有 REFERENCES，能塞进去
 ('WO-SB-002', 'CX-999', '未知', 'other', 'low', '待查', '[]', 'k-999-other', '待处理', '设备科');

COMMIT;
