-- 007_biz_seed.sql —— 业务种子（可重复跑：先清空再灌）
-- 量：工单 60 / 检验批次 80 / 缺陷记录 300 / 返修单 40 / 报警 120
--
-- 缺陷记录里有 10 行是 defect_records.csv 的原样拷贝（rec_id 1~9 与 9001，值逐字照抄）。
-- 这样同一批行既是库里的一条记录、也是 CSV 的一行，T6 能逐行对比两条路。

TRUNCATE biz_alarm, biz_repair_order, biz_defect_record, biz_inspection_lot, biz_work_order;

-- 工单：四条线各 15 张，状态按五词循环，结束时间只在闭环态才有
INSERT INTO biz_work_order (wo_no, station_no, equip_no, opened_at, closed_at, state, stage_id, priority, maintenance_team_id)
SELECT 'WO-2026-' || lpad(i::text, 4, '0'),
       (((i-1) % 4) + 1) || '号工位',
       'CX-' || lpad((101 + (i-1) % 48)::text, 3, '0'),
       '2026-01-01 08:00+08'::timestamptz + ((i-1)*7 || ' hours')::interval,
       CASE WHEN (ARRAY['待处理','处理中','已完成','已合并','已取消'])[(i-1)%5+1] IN ('已完成','已合并','已取消')
            THEN '2026-01-01 08:00+08'::timestamptz + ((i-1)*7 || ' hours')::interval + ((2 + i % 6) || ' hours')::interval
            ELSE NULL END,
       (ARRAY['待处理','处理中','已完成','已合并','已取消'])[(i-1)%5+1],
       (ARRAY['待排产','进行中','待验收','已关闭'])[(i-1)%4+1],
       (i % 4)::smallint,
       'TEAM-' || ((i % 3) + 1)
FROM generate_series(1, 60) AS i;

-- 故意留三张"两套状态打架"：state 已结束，stage 还停在中间
UPDATE biz_work_order SET stage_id = '进行中'
WHERE wo_no IN ('WO-2026-0003', 'WO-2026-0008', 'WO-2026-0023');

-- 检验批次
INSERT INTO biz_inspection_lot (lot_no, line_no, equip_no, shift, started_at, ended_at, sample_qty, defect_qty, pass_rate)
SELECT 'LOT-' || chr(64 + ((i-1)/20 + 1)) || '-' || to_char(date '2026-03-01' + ((i-1)/10)::int, 'YYYYMMDD') || '-' || lpad(((i-1)%10+1)::text, 2, '0'),
       '产线' || chr(64 + ((i-1)/20 + 1)),
       'CX-' || lpad((101 + (i-1) % 48)::text, 3, '0'),
       (ARRAY['早','中','夜'])[(i-1)%3+1],
       '2026-03-01 08:00+08'::timestamptz + ((i-1)*3 || ' hours')::interval,
       '2026-03-01 08:00+08'::timestamptz + ((i-1)*3 || ' hours')::interval + '8 hours',
       20 + (i % 5) * 10,
       i % 7,
       round((((20 + (i % 5) * 10) - (i % 7))::numeric / (20 + (i % 5) * 10)) * 100, 2)
FROM generate_series(1, 80) AS i;

-- 缺陷记录：290 条生成 + 10 条原样拷贝
INSERT INTO biz_defect_record (rec_id, captured_at, equip_no, work_station, defect_name, conf_pct, lot_no, judge, is_ok)
SELECT 1000 + i,
       '2026-04-01 06:00+08'::timestamptz + ((i-1)*5 || ' hours')::interval,
       'CX-' || lpad((101 + (i-1) % 48)::text, 3, '0'),
       (((i-1) % 4) + 1) || '号工位',
       (SELECT defect_name FROM md_defect_code WHERE defect_code = 'D-' || lpad((((i-1) % 30) + 1)::text, 3, '0')),
       ((i % 40) + 60)::numeric,
       'B' || to_char(date '2026-04-01' + ((i-1)/24)::int, 'YYYYMMDD'),
       CASE WHEN (i % 40) + 60 >= 80 THEN 'NG' ELSE 'OK' END,
       CASE WHEN (i % 40) + 60 >= 80 THEN 'N' ELSE 'Y' END
FROM generate_series(1, 290) AS i;

-- 这 10 行逐字照抄 defect_records.csv：脏值一个不改，包括 2027 的未来时间、150 与 -10、空 lot_no、'A'、CX-999
INSERT INTO biz_defect_record VALUES
 (1,   '2026-11-01 08:00+08', 'CX-101', '2号工位', '划伤',  91, 'B20261101', 'NG', 'N'),
 (2,   '2026-11-01 09:00+08', 'CX-101', '2号工位', '划伤',  88, 'B20261101', 'NG', 'N'),
 (3,   '2026-11-01 10:00+08', 'CX-102', '2号工位', '污点',  75, 'B20261101', 'OK', 'Y'),
 (4,   '2027-01-01 08:00+08', 'CX-101', '2号工位', '划伤',  90, 'B20261101', 'NG', 'N'),
 (5,   '2026-11-02 08:00+08', 'CX-101', '2号工位', '划伤', 150, 'B20261102', 'NG', 'N'),
 (6,   '2026-11-02 09:00+08', 'CX-102', '2号工位', '污点', -10, 'B20261102', 'OK', 'Y'),
 (7,   '2026-11-02 10:00+08', 'CX-101', '4号线',   '划伤',  92, 'B20261102', 'NG', 'N'),
 (8,   '2026-11-03 08:00+08', 'CX-102', '2号工位', '污点',  80, '',          'OK', 'Y'),
 (9,   '2026-11-03 09:00+08', 'CX-102', '2号工位', '污点',  81, 'A',         'OK', 'Y'),
 (9001,'2026-11-04 08:00+08', 'CX-999', '2号工位', '划伤',  88, 'B20261104', 'NG', 'N');

-- 返修单：只挂在生成的缺陷记录上
INSERT INTO biz_repair_order (ro_no, rec_id, equip_no, opened_at, closed_at, state, handler)
SELECT 'RO-2026-' || lpad(i::text, 3, '0'),
       1000 + (i * 7 % 290) + 1,
       'CX-' || lpad((101 + (i-1) % 48)::text, 3, '0'),
       '2026-05-01 08:00+08'::timestamptz + ((i-1)*9 || ' hours')::interval,
       CASE WHEN (ARRAY['待处理','处理中','已完成','已合并','已取消'])[(i-1)%5+1] IN ('已完成','已合并','已取消')
            THEN '2026-05-01 08:00+08'::timestamptz + ((i-1)*9 || ' hours')::interval + ((3 + i % 5) || ' hours')::interval
            ELSE NULL END,
       (ARRAY['待处理','处理中','已完成','已合并','已取消'])[(i-1)%5+1],
       (ARRAY['张伟','李强','王芳','赵敏'])[(i-1)%4+1]
FROM generate_series(1, 40) AS i;

-- 报警：每三台有一台没清
INSERT INTO biz_alarm (alarm_id, equip_no, raised_at, cleared_at, code, level, acked_by)
SELECT i,
       'CX-' || lpad((101 + (i-1) % 48)::text, 3, '0'),
       '2026-06-01 00:00+08'::timestamptz + ((i-1)*4 || ' hours')::interval,
       CASE WHEN i % 3 = 0 THEN NULL
            ELSE '2026-06-01 00:00+08'::timestamptz + ((i-1)*4 + 2 || ' hours')::interval END,
       (ARRAY['AL-01','AL-02','AL-03','AL-04'])[(i-1)%4+1],
       ((i-1) % 3 + 1)::smallint,
       CASE WHEN i % 3 = 0 THEN NULL ELSE (ARRAY['张伟','李强','王芳'])[(i-1)%3+1] END
FROM generate_series(1, 120) AS i;