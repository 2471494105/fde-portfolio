-- 003_schema.sql —— 骨架与授权（在 fde_client 库执行，身份 postgres）
--
-- 三条分工，别混：
--   postgres  扮演客户 DBA：能 DDL、能建表。变更模拟器（T8）用它。
--   qms_app   扮演客户业务系统：只能 DML，CREATE 必须被拒——"应用改不了结构"是这套系统的前提。
--   ro_reader 扮演开给我们的只读账号：只能 SELECT。
--   auditor   只看得见 aud schema，连 public 的 USAGE 都不给。
--
-- 业务表必须留在 public：观测器（check_source_schema）写的谓词是 table_schema='public'，
-- 换到别的 schema 它当场查空，S1–S4 会变成假绿（"没看到列=没变更"）。

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO qms_app, ro_reader;

CREATE SCHEMA IF NOT EXISTS gov AUTHORIZATION postgres;   -- 字段登记册 / 变更单 / 审批
CREATE SCHEMA IF NOT EXISTS aud AUTHORIZATION postgres;   -- 审计流水
REVOKE ALL ON SCHEMA gov FROM PUBLIC;
REVOKE ALL ON SCHEMA aud FROM PUBLIC;
GRANT USAGE ON SCHEMA gov TO qms_app;
GRANT USAGE ON SCHEMA aud TO qms_app, auditor;

-- 以后 postgres 在 public 建的每一张表，自动落这三档权限；不用逐表 GRANT
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT ON TABLES TO ro_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO qms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO qms_app;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA gov
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO qms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA gov
  GRANT USAGE, SELECT ON SEQUENCES TO qms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA aud
  GRANT INSERT ON TABLES TO qms_app;      -- 业务账号只能写，不能读自己留下的审计
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA aud
  GRANT USAGE, SELECT ON SEQUENCES TO qms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA aud
  GRANT SELECT ON TABLES TO auditor;

-- 已存在的表/序列也要跟上（ALTER DEFAULT PRIVILEGES 只管以后建的）
REVOKE SELECT ON ALL TABLES IN SCHEMA aud FROM qms_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA gov TO qms_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA aud TO qms_app;

-- 连库权限收回到具体账号：一个没被授权的账号连不上，这是"客户库"该有的样子
REVOKE ALL ON DATABASE fde_client FROM PUBLIC;
GRANT CONNECT ON DATABASE fde_client TO qms_app, ro_reader, auditor;