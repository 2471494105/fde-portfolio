-- 001_roles.sql —— 三个账号（集群级，在 postgres 库执行）
-- 密码只用于本地演示；真实环境从密钥管理取，绝不写进 SQL。
-- 角色分工是这套系统要演示的第一件事：一台库里同时存在"能写""只能读""只能看审计"三种人。

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qms_app') THEN
    EXECUTE 'CREATE ROLE qms_app LOGIN PASSWORD ''qmsapp2026pw''';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ro_reader') THEN
    EXECUTE 'CREATE ROLE ro_reader LOGIN PASSWORD ''roreader2026pw''';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'auditor') THEN
    EXECUTE 'CREATE ROLE auditor LOGIN PASSWORD ''auditor2026pw''';
  END IF;
END
$$;

COMMENT ON ROLE qms_app   IS '客户业务系统账号：只能 DML，不能改结构';
COMMENT ON ROLE ro_reader IS '开给我们的只读账号：只能 SELECT';
COMMENT ON ROLE auditor   IS '审计员：只看 aud schema，看不见业务明细';