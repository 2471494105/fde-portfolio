-- 008_gov.sql —— 治理四表（在 fde_client 库执行，身份 postgres = 客户 DBA）
--
-- gov.field_registry   字段登记册：一行一个字段，能按列 diff、按列查谁登记的
-- gov.change_request   变更单：谁要改、改成什么、为什么
-- gov.approval         审批：谁批的、什么时候、批的什么
-- aud.audit_log        审计：操作前后值（T7 挂触发器，本步只建表）
--
-- 为什么登记册不存一整块 JSON：整块 blob 只能整体比对，出问题时说不出"哪一列在第几版变的"。

CREATE SCHEMA IF NOT EXISTS gov;

CREATE TABLE IF NOT EXISTS gov.field_registry (
  registry_id   bigserial PRIMARY KEY,
  client        text NOT NULL,
  table_name    text NOT NULL,
  column_name   text NOT NULL,
  ordinal       smallint NOT NULL,
  data_type     text,
  is_nullable   boolean NOT NULL,
  is_key        boolean NOT NULL DEFAULT false,
  observed_via  text NOT NULL,                  -- db_metadata / csv_header / http_keys
  unit          text,
  domain_kind   text CHECK (domain_kind IN ('range','format','enum','allow_empty','null')),
  domain_spec   jsonb,
  note          text,
  version       smallint NOT NULL DEFAULT 1,
  is_active     boolean NOT NULL DEFAULT true,
  registered_at timestamptz NOT NULL DEFAULT now(),
  registered_by text NOT NULL,
  UNIQUE (client, table_name, column_name, version)
);
COMMENT ON TABLE gov.field_registry IS '字段登记册：机器可读的列级契约';

CREATE TABLE IF NOT EXISTS gov.change_request (
  req_no       text PRIMARY KEY,
  client       text NOT NULL,
  table_name   text NOT NULL,
  change_kind  text NOT NULL CHECK (change_kind IN
                 ('add_column','drop_column','rename_column','change_type','change_domain','change_state')),
  column_name  text,
  payload      jsonb,
  reason       text NOT NULL,
  requested_by text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected','executed')),
  decided_at   timestamptz,
  executed_at  timestamptz
);
COMMENT ON TABLE gov.change_request IS '变更单：改动必须留原因和申请人';

CREATE TABLE IF NOT EXISTS gov.approval (
  approval_id bigserial PRIMARY KEY,
  req_no      text NOT NULL REFERENCES gov.change_request(req_no),
  approver    text NOT NULL,
  role        text NOT NULL,
  decision    text NOT NULL CHECK (decision IN ('approve','reject')),
  decided_at  timestamptz NOT NULL DEFAULT now(),
  comment     text
);
COMMENT ON TABLE gov.approval IS '审批：一张变更单可以有多条审批（多级）';

CREATE TABLE IF NOT EXISTS aud.audit_log (
  audit_id     bigserial PRIMARY KEY,
  table_name   text NOT NULL,
  op           text NOT NULL CHECK (op IN ('insert','update','delete','ddl')),
  row_key      text,
  before_value jsonb,
  after_value  jsonb,
  actor        text NOT NULL,
  at           timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE aud.audit_log IS '审计流水：只追加，业务账号写不进读不出';

-- 登记册：从元数据读出来，不手打（手打的登记册就是第二份真相）
DELETE FROM gov.field_registry WHERE client = 'client_mes';
INSERT INTO gov.field_registry (client, table_name, column_name, ordinal, data_type, is_nullable, is_key, observed_via, registered_by)
SELECT 'client_mes',
       c.table_name,
       c.column_name,
       c.ordinal_position::smallint,
       c.data_type,
       (c.is_nullable = 'YES'),
       (c.column_name = ANY (
         CASE c.table_name
           WHEN 'md_device'         THEN ARRAY['equip_no']
           WHEN 'biz_defect_record' THEN ARRAY['rec_id']
           WHEN 'biz_work_order'    THEN ARRAY['wo_no']
           ELSE ARRAY[]::text[] END)),
       'db_metadata',
       'sql/008_gov.sql'
FROM information_schema.columns c
WHERE c.table_schema = 'public'
  AND c.table_name IN ('md_device','biz_defect_record','biz_work_order');

-- 值域：只登记真的能从库里读出来的口径；拿不准的写 note，不瞎填
UPDATE gov.field_registry SET is_key = true,
       domain_kind = 'format', domain_spec = '{"regex":"^CX-[0-9]{3}$"}'::jsonb,
       note = '库里有 CHECK 约束；客户 CSV 里出现过小写 cx-101，两路不一致'
WHERE table_name = 'md_device' AND column_name = 'equip_no';

UPDATE gov.field_registry SET unit = 'day',
       domain_kind = 'range', domain_spec = '{"min":1,"max":3650}'::jsonb
WHERE table_name = 'md_device' AND column_name = 'maint_cycle';

UPDATE gov.field_registry SET domain_kind = 'format', domain_spec = '{"format":"%Y-%m-%d"}'::jsonb,
       note = '库直连按 ISO 渲染；同一列的客户 CSV 是 %Y/%m/%d，两条路不一样'
WHERE table_name = 'md_device' AND column_name = 'install_dt';

UPDATE gov.field_registry SET domain_kind = 'format', domain_spec = '{"format":"%Y-%m-%d %H:%M"}'::jsonb
WHERE table_name = 'biz_defect_record' AND column_name = 'captured_at';

UPDATE gov.field_registry SET unit = 'percent',
       domain_kind = 'range',
       domain_spec = '{"min":0,"max":100,"severity":"yellow","why":"真样本有 150 与 -10 各一条"}'::jsonb
WHERE table_name = 'biz_defect_record' AND column_name = 'conf_pct';

UPDATE gov.field_registry SET domain_kind = 'enum', domain_spec = '{"values":["NG","OK"]}'::jsonb
WHERE table_name = 'biz_defect_record' AND column_name = 'judge';

UPDATE gov.field_registry SET domain_kind = 'enum', domain_spec = '{"values":["N","Y"]}'::jsonb
WHERE table_name = 'biz_defect_record' AND column_name = 'is_ok';

UPDATE gov.field_registry SET domain_kind = 'enum',
       domain_spec = '{"values":["待处理","处理中","已完成","已合并","已取消"],"severity":"breaking","why":"口径词表，改一个就是换版本"}'::jsonb,
       note = '库里没设 CHECK；词表只能靠判据比'
WHERE table_name = 'biz_work_order' AND column_name = 'state';

UPDATE gov.field_registry SET domain_kind = 'enum', domain_spec = '{"values":["待排产","进行中","待验收","已关闭"]}'::jsonb,
       note = '看板阶段，不是业务状态；两者会漂移'
WHERE table_name = 'biz_work_order' AND column_name = 'stage_id';

-- 两张示例变更单：一张已批、一张待批，用来证明审批链能查
INSERT INTO gov.change_request (req_no, client, table_name, change_kind, column_name, payload, reason, requested_by, requested_at, status, decided_at)
VALUES
  ('CR-2026-0001','client_mes','md_device','add_column','torque_limit',
   '{"type":"numeric","nullable":true,"unit":"Nm"}'::jsonb,
   '产线要按扭矩报警，现台账没有这个字段','现场工程师 张伟','2026-11-20 09:40+08','approved','2026-11-20 11:30+08'),
  ('CR-2026-0002','client_mes','biz_defect_record','rename_column','defect_name',
   '{"new_name":"defect_code","map":{"划伤":"D-001","污点":"D-002"}}'::jsonb,
   '缺陷名手打会错字，改存字典码','质量主管 李强','2026-11-21 14:10+08','pending',NULL);

INSERT INTO gov.approval (req_no, approver, role, decision, decided_at, comment)
VALUES ('CR-2026-0001','DBA 王芳','dba','approve','2026-11-20 11:30+08','同意，先加可空列，不锁表');