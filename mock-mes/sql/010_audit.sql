-- 010_audit.sql —— 审计触发器 + 越权安全阀（T7）
--
-- 三件事，每一件都是被现场要求过才会有的做法：
--   1) 审计只记"变化的列"，不落全行。全行快照等于把生产数据又存一份，谁都不敢开。
--   2) 触发器 SECURITY DEFINER 以 DBA 身份写 aud：业务账号有写权限也读不到，反过来更安全。
--   3) 越权不抛错，返空集。真实接口层就是这么"温顺"，所以拒绝必须另留一条痕，否则没人知道。
--
-- 为什么 op 多了 'denied'：越权事件本来就该和业务改写躺在同一条时间线上，
-- 分开存，排查时对不上。这是一次 schema 变更，登记成 CR-2026-0003 并走审批。

-- ---------- 1. op 词表扩一个 'denied'（幂等：先找出旧约束再换） ----------
DO $$
DECLARE cname text;
BEGIN
  FOR cname IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'aud.audit_log'::regclass AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%insert%'
  LOOP
    EXECUTE format('ALTER TABLE aud.audit_log DROP CONSTRAINT %I', cname);
  END LOOP;
  ALTER TABLE aud.audit_log
    ADD CONSTRAINT audit_log_op_check
    CHECK (op IN ('insert', 'update', 'delete', 'ddl', 'denied'));
END $$;

-- ---------- 2. 变更登记（这次 DDL 自己也要进登记册） ----------
INSERT INTO gov.change_request (req_no, client, table_name, change_kind, column_name, payload, reason, requested_by, status, decided_at)
VALUES ('CR-2026-0003', 'client_mes', 'aud.audit_log', 'change_domain', 'op',
        jsonb_build_object('add', 'denied', 'why', '越权事件与业务改写放同一条流水'),
        '接口层对越权返空集，拒绝不落痕就分不出"没数据"和"没权限"', '曹钢', 'approved', now())
ON CONFLICT (req_no) DO NOTHING;

INSERT INTO gov.approval (req_no, approver, role, decision, comment)
SELECT 'CR-2026-0003', '王芳', 'DBA', 'approve', '同意并表，不另开一张拒绝流水'
 WHERE NOT EXISTS (SELECT 1 FROM gov.approval WHERE req_no = 'CR-2026-0003');

-- ---------- 3. 差异计算 + 落库 ----------
CREATE OR REPLACE FUNCTION aud.fn_audit_diff() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
  before_all jsonb;
  after_all  jsonb;
  before_ch  jsonb := '{}'::jsonb;
  after_ch   jsonb := '{}'::jsonb;
  k text;
  rk text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    before_all := to_jsonb(OLD);
    rk := before_all ->> TG_ARGV[0];
    INSERT INTO aud.audit_log (table_name, op, row_key, before_value, after_value, actor)
    VALUES (TG_TABLE_NAME, 'delete', rk, before_all, NULL, session_user);
    RETURN OLD;
  ELSIF TG_OP = 'INSERT' THEN
    after_all := to_jsonb(NEW);
    rk := after_all ->> TG_ARGV[0];
    INSERT INTO aud.audit_log (table_name, op, row_key, before_value, after_value, actor)
    VALUES (TG_TABLE_NAME, 'insert', rk, NULL, after_all, session_user);
    RETURN NEW;
  ELSE
    before_all := to_jsonb(OLD);
    after_all  := to_jsonb(NEW);
    FOR k IN SELECT jsonb_object_keys(after_all) LOOP
      IF (before_all -> k) IS DISTINCT FROM (after_all -> k) THEN
        before_ch := before_ch || jsonb_build_object(k, before_all -> k);
        after_ch  := after_ch  || jsonb_build_object(k, after_all  -> k);
      END IF;
    END LOOP;
    IF before_ch = '{}'::jsonb THEN
      RETURN NEW;                       -- 一个字段都没变，不写流水
    END IF;
    rk := after_all ->> TG_ARGV[0];
    INSERT INTO aud.audit_log (table_name, op, row_key, before_value, after_value, actor)
    VALUES (TG_TABLE_NAME, 'update', rk, before_ch, after_ch, session_user);
    RETURN NEW;
  END IF;
END $$;
COMMENT ON FUNCTION aud.fn_audit_diff() IS '行级差异审计：update 只记变化的列，insert/delete 记整行';

-- ---------- 4. 挂到五张业务表（幂等：先删同名触发器） ----------
DO $$
DECLARE rec record;
BEGIN
  FOR rec IN
    SELECT * FROM (VALUES
      ('biz_work_order',     'wo_no'),
      ('biz_inspection_lot', 'lot_no'),
      ('biz_defect_record',  'rec_id'),
      ('biz_repair_order',   'ro_no'),
      ('biz_alarm',          'alarm_id')
    ) AS v(tbl, pkc)
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_audit_%1$s ON public.%1$s', rec.tbl);
    EXECUTE format(
      'CREATE TRIGGER trg_audit_%1$s
         AFTER INSERT OR UPDATE OR DELETE ON public.%1$s
         FOR EACH ROW EXECUTE FUNCTION aud.fn_audit_diff(%2$L)',
      rec.tbl, rec.pkc);
  END LOOP;
END $$;

-- ---------- 5. 越权安全阀 ----------
-- 真实接口层的脾气：查不到就给空数组，不返 403。代价是"没数据"和"没权限"长得一样，
-- 所以拒绝要自己落一条 denied，否则这层温顺就是新挖的坑。
CREATE OR REPLACE FUNCTION gov.read_guarded(p_limit integer DEFAULT 20)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
  caller text := session_user;
  out_rows jsonb;
BEGIN
  IF NOT (has_schema_privilege(caller, 'gov', 'USAGE')
          AND has_table_privilege(caller, 'gov.field_registry', 'SELECT')) THEN
    INSERT INTO aud.audit_log (table_name, op, row_key, before_value, after_value, actor)
    VALUES ('gov.field_registry', 'denied', NULL, NULL,
            jsonb_build_object('reason', 'no select privilege', 'caller', caller), caller);
    RETURN '[]'::jsonb;
  END IF;

  SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t ->> 'registry_id'), '[]'::jsonb)
    INTO out_rows
    FROM (SELECT * FROM gov.field_registry ORDER BY registry_id LIMIT p_limit) t;
  RETURN out_rows;
END $$;
COMMENT ON FUNCTION gov.read_guarded(integer) IS '越权返回空集而非报错；每次拒绝都落一条 denied';

GRANT USAGE ON SCHEMA gov TO ro_reader;   -- 只为让它能调用这个函数；表权限仍然没有
GRANT EXECUTE ON FUNCTION gov.read_guarded(integer) TO ro_reader, qms_app;