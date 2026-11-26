-- 004_master.sql —— 主数据六表（在 fde_client 库执行，身份 postgres = 客户 DBA）
--
-- 列名照抄现场样本（equipment.csv 的八列原样保留），这样同一张表既有 CSV 形态又有库形态，
-- 判据不用改写就能对两种形态同时成立。
--
-- 约束是有意的：
--   ck_equip_no 要求大写 CX-三位数字。现场 CSV 里有 cx-101 这种小写行——
--   库比导出严，这正好是"同一份数据两条路不一样"的第一处证据。
--   maint_cycle BETWEEN 1 AND 3650 同理，越界值插不进来。

CREATE TABLE IF NOT EXISTS md_line (
  line_no    text PRIMARY KEY,                 -- 产线A
  line_name  text NOT NULL,
  workshop   text NOT NULL,
  is_active  boolean NOT NULL DEFAULT true,
  CONSTRAINT ck_line_no CHECK (line_no <> '')
);
COMMENT ON TABLE  md_line IS '产线主数据';
COMMENT ON COLUMN md_line.line_no IS '产线编码，业务表引用它';

CREATE TABLE IF NOT EXISTS md_station (
  station_no   text PRIMARY KEY,               -- 产线A-01
  line_no      text NOT NULL REFERENCES md_line(line_no),
  station_name text NOT NULL,
  seq          smallint NOT NULL CHECK (seq > 0),
  UNIQUE (line_no, seq)
);
COMMENT ON TABLE md_station IS '工位主数据，一产线多工位';

CREATE TABLE IF NOT EXISTS md_device (
  equip_no     text PRIMARY KEY,               -- CX-101（与 equipment.csv 同名列名）
  prod_line    text NOT NULL REFERENCES md_line(line_no),
  work_station text NOT NULL REFERENCES md_station(station_no),
  cam_model    text NOT NULL,
  lens_spec    text NOT NULL,
  lamp_type    text NOT NULL,
  install_dt   date NOT NULL,
  maint_cycle  smallint NOT NULL CHECK (maint_cycle BETWEEN 1 AND 3650),
  CONSTRAINT ck_equip_no CHECK (equip_no ~ '^CX-[0-9]{3}$')
);
COMMENT ON TABLE  md_device IS '视觉检测设备台账';
COMMENT ON COLUMN md_device.install_dt  IS '安装日期（date 类型；导出成文本时由渲染决定格式）';
COMMENT ON COLUMN md_device.maint_cycle IS '保养周期，天';

CREATE TABLE IF NOT EXISTS md_check_item (
  item_no     text PRIMARY KEY,                -- CI-01
  item_name   text NOT NULL,
  unit        text,                            -- mm / pct / 次，可为空
  lower_limit numeric,
  upper_limit numeric,
  is_active   boolean NOT NULL DEFAULT true,
  CONSTRAINT ck_item_range CHECK (
    lower_limit IS NULL OR upper_limit IS NULL OR lower_limit <= upper_limit
  )
);
COMMENT ON TABLE md_check_item IS '检验项定义（含上下限，供值域检查对齐）';

CREATE TABLE IF NOT EXISTS md_defect_code (
  defect_code text PRIMARY KEY,                -- D-001
  defect_name text NOT NULL,
  category    text NOT NULL CHECK (category IN ('外观','尺寸','功能','装配')),
  severity    smallint NOT NULL CHECK (severity BETWEEN 1 AND 3),
  is_active   boolean NOT NULL DEFAULT true
);
COMMENT ON TABLE md_defect_code IS '缺陷码字典，category 四类固定';

CREATE TABLE IF NOT EXISTS md_material (
  material_no   text PRIMARY KEY,
  material_name text NOT NULL,
  spec          text,
  unit          text NOT NULL DEFAULT 'pcs'
);
COMMENT ON TABLE md_material IS '物料主数据';