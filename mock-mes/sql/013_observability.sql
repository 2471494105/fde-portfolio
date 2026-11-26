-- 013_observability.sql —— 判据运行台账：谁在什么时候跑过、红了几条
--
-- 可观测性的第一条不是「有图表」，是「这件事到底有没有发生过」。
-- 判据红一次只是终端里的一屏字，下一次运行就把它覆盖了；没有台账时，
-- 「昨天是不是红的」只能靠回忆。所以每次跑判据落一行：
--
--   check_id / ran_at / duration_ms / status(ok|warn|fail|error) / red_count / dataset / details
--
-- 台账本身也要能被观测：页面会报「最近一次运行是多久以前」，
-- 超过 24 小时没有记录就当红——监控自己静默，比数据静默更危险。
--
-- 放在 public 而不是 gov：003 的 ALTER DEFAULT PRIVILEGES 只覆盖 public，
-- 这里要借它自动给 ro_reader 只读权限，不另写逐表 GRANT。

CREATE TABLE IF NOT EXISTS gov_check_run (
  run_id      bigserial PRIMARY KEY,
  check_id    text NOT NULL,                        -- status_policy / station_contract
  ran_at      timestamptz NOT NULL DEFAULT now(),
  duration_ms integer NOT NULL CHECK (duration_ms >= 0),
  status      text NOT NULL CHECK (status IN ('ok', 'warn', 'fail', 'error')),
  red_count   integer NOT NULL DEFAULT 0 CHECK (red_count >= 0),
  dataset     text,                                 -- 观测面：fde_client / 表名
  code_hash   text,
  details     jsonb
);

CREATE INDEX IF NOT EXISTS ix_check_run_recent ON gov_check_run (check_id, ran_at DESC);

COMMENT ON TABLE gov_check_run IS '判据运行台账：每次运行一行，供观测页与趋势使用';
COMMENT ON COLUMN gov_check_run.red_count IS '这一轮红了多少（口径随 check：口径判据=失败条数，工位契约=违约行合计），details 里写清';
COMMENT ON COLUMN gov_check_run.status IS 'ok 全绿 / warn 有认领中的黄 / fail 有红 / error 脚本自身崩';
