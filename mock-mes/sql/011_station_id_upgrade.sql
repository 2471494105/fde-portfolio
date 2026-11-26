-- 011_station_id_upgrade.sql —— 工位唯一 id：契约的一部分，不是后端兜底
--
-- 背景：工单写「1号工位」，主数据写「产线A-01」，同一事实两套命名。
-- 后端的正确姿势不是「用设备反推工位」（那是猜，且实测 40/60 与设备台账矛盾），
-- 而是：主数据侧给一个稳定键 station_id，明细侧按契约提交它、或者提交「产线 + 工位号」；
-- 提交不全就判红退回，由产线补数据。
--
-- 本文件对已有库幂等：列已存在时 ALTER 是空操作，回填也只补 NULL。
-- 新装（004/005/006）已经带这些列，所以两边装出来的结构一致。

ALTER TABLE md_station ADD COLUMN IF NOT EXISTS station_id text;
UPDATE md_station
   SET station_id = 'S-' || right(line_no, 1) || lpad(seq::text, 2, '0')
 WHERE station_id IS NULL;              -- 产线A 第 1 工位 → S-A01
ALTER TABLE md_station ALTER COLUMN station_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_md_station_id ON md_station (station_id);
COMMENT ON COLUMN md_station.station_id IS '工位内部稳定键（S-A01）；改名不动外键；对外仍用 station_no';

-- 明细侧：契约要求 station_id，或者 (prod_line + 工位号) 让判据能查字典解析。
-- 两者都给不出来 = 违约，判红退回产线补，不由后端推导。
ALTER TABLE biz_work_order   ADD COLUMN IF NOT EXISTS station_id text;
ALTER TABLE biz_work_order   ADD COLUMN IF NOT EXISTS prod_line  text;
ALTER TABLE biz_defect_record ADD COLUMN IF NOT EXISTS station_id text;
ALTER TABLE biz_defect_record ADD COLUMN IF NOT EXISTS prod_line  text;

COMMENT ON COLUMN biz_work_order.station_no    IS '工位号的现场写法（N号工位），缺产线时无法唯一归属';
COMMENT ON COLUMN biz_work_order.station_id    IS '契约字段：工位稳定键，由产线填；为空表示未按契约提交';
COMMENT ON COLUMN biz_work_order.prod_line     IS '契约字段：产线编码，与 station_no 合用可解析出 station_id';
