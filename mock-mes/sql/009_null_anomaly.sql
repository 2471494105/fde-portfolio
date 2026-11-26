-- 009_null_anomaly.sql —— 补两条"库里形态的空值"（必须在 007_biz_seed.sql 之后跑）
--
-- 为什么单独一个文件：CSV 里空是空串 ""，库里空可以是 NULL，两者不是一回事。
-- rec_id=8 那条是从 CSV 抄来的空串，rec_id=10/20 这两条是库里才有的 NULL。
-- 判据要能分清"空"的两种表示，否则"允许空"这条声明永远判不准。
--
-- 位置是挑过的：按 rec_id 升序，size=10 时第 1 页首行是 rec_id=1（lot_no 有值），
-- 第 2 页首行是 rec_id=20（lot_no 与 conf_pct 都是 NULL），分页抖动才复现得出来。

INSERT INTO biz_defect_record (rec_id, captured_at, equip_no, work_station, defect_name, conf_pct, lot_no, judge, is_ok)
VALUES
  (10, '2026-11-05 08:00+08', 'CX-103', '3号工位', '划伤', 87, NULL, 'NG', 'N'),
  (20, '2026-11-06 08:00+08', 'CX-104', NULL,      '污点', NULL, 'B20261106', 'OK', 'Y')
ON CONFLICT (rec_id) DO NOTHING;