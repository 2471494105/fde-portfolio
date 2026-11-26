-- 006_biz.sql —— 业务五表（在 fde_client 库执行，身份 postgres = 客户 DBA）
--
-- 三条故意为之，都是给判据留活口，不是偷懒：
--   1) state 不设 CHECK。词表是契约，契约要靠判据去比，库先拦掉就永远测不到"未登记状态"。
--   2) conf_pct 不设 0-100 约束。150 和 -10 必须能进库，否则值域抽查没有对象。
--   3) 业务表不挂外键到主数据。CX-999 这种孤儿引用在客户系统里真实存在，挂了 FK 就复现不出来。
--
-- biz_work_order 同时有 state 和 stage_id：照 Odoo maintenance.request 的真实结构抄的，
-- 两个字段都长得像"状态"，判据很容易抓错——这正是"同一份事实存两份"的现场版本。

CREATE TABLE IF NOT EXISTS biz_work_order (
  wo_no              text PRIMARY KEY,             -- WO-2026-0001
  station_no         text NOT NULL,                -- 2号工位（现场口径，与 md_station 的产线A-01 不同）
  station_id         text,                         -- 契约字段：工位稳定键，产线填；空 = 未按契约提交
  prod_line          text,                         -- 契约字段：与 station_no 合用可解析出 station_id
  equip_no           text NOT NULL,
  opened_at          timestamptz NOT NULL,
  closed_at          timestamptz,
  state              text NOT NULL,                -- 待处理/处理中/已完成/已合并/已取消
  stage_id           text NOT NULL,                -- 看板阶段：待排产/进行中/待验收/已关闭
  priority           smallint NOT NULL CHECK (priority BETWEEN 0 AND 3),
  maintenance_team_id text NOT NULL,
  note               text
);
COMMENT ON TABLE  biz_work_order IS '维修工单';
COMMENT ON COLUMN biz_work_order.state    IS '业务状态（契约词表，故意不设 CHECK）';
COMMENT ON COLUMN biz_work_order.stage_id IS '看板阶段（与 state 并存的第二套状态，会漂移）';

CREATE TABLE IF NOT EXISTS biz_inspection_lot (
  lot_no     text PRIMARY KEY,                     -- LOT-A-20260301-01
  line_no    text NOT NULL,
  equip_no   text NOT NULL,
  shift      text NOT NULL,                        -- 早/中/夜
  started_at timestamptz NOT NULL,
  ended_at   timestamptz,
  sample_qty integer NOT NULL CHECK (sample_qty > 0),
  defect_qty integer NOT NULL CHECK (defect_qty >= 0),
  pass_rate  numeric
);
COMMENT ON TABLE biz_inspection_lot IS '检验批次';

CREATE TABLE IF NOT EXISTS biz_defect_record (
  rec_id      bigint PRIMARY KEY,                  -- 与 defect_records.csv 的 rec_id 同一编号
  captured_at timestamptz NOT NULL,
  equip_no    text NOT NULL,
  work_station text,
  station_id  text,                                -- 契约字段：工位稳定键，产线填；空 = 未按契约提交
  prod_line   text,                                -- 契约字段：与 work_station 合用可解析出 station_id
  defect_name text NOT NULL,                       -- 存名字不存码：真实系统常见，口径问题
  conf_pct    numeric,                             -- 百分制；故意不设 0-100
  lot_no      text,                                -- 允许空
  judge       text NOT NULL,                       -- NG/OK
  is_ok       text NOT NULL                        -- N/Y
);
COMMENT ON TABLE  biz_defect_record IS '缺陷记录，九列与 defect_records.csv 一一对应';
COMMENT ON COLUMN biz_defect_record.defect_name IS '缺陷名（不是缺陷码，字典在 md_defect_code）';

CREATE TABLE IF NOT EXISTS biz_repair_order (
  ro_no     text PRIMARY KEY,                      -- RO-2026-001
  rec_id    bigint NOT NULL,                       -- 指向缺陷记录（软引用，无 FK）
  equip_no  text NOT NULL,
  opened_at timestamptz NOT NULL,
  closed_at timestamptz,
  state     text NOT NULL,                         -- 与工单同一套词表
  handler   text
);
COMMENT ON TABLE biz_repair_order IS '返修单';

CREATE TABLE IF NOT EXISTS biz_alarm (
  alarm_id   integer PRIMARY KEY,
  equip_no   text NOT NULL,
  raised_at  timestamptz NOT NULL,
  cleared_at timestamptz,
  code       text NOT NULL,                        -- AL-01..AL-04
  level      smallint NOT NULL CHECK (level BETWEEN 1 AND 3),
  acked_by   text
);
COMMENT ON TABLE biz_alarm IS '设备报警';