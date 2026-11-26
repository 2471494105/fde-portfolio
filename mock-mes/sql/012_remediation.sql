-- 012_remediation.sql —— 数据整改单：判据红了，得有一张能被追的纸
--
-- 为什么放治理侧：判据红只是一次输出，没人认领的红会被下一次运行覆盖掉。
-- 整改单把「哪张表、哪个字段、多少行、样例单号、依据哪条契约、谁负责、什么时候到期」固化下来；
-- 复跑违规归零才允许关闭——**纸面不能关闭数据问题**。
--
-- state 只两种：open / closed。过期不自动改状态（那会让「到期」这件事被系统替人遗忘），
-- 由判据 J 在到期日之后报红。

CREATE TABLE IF NOT EXISTS gov_remediation_order (
  rm_no             text PRIMARY KEY,              -- RM-20261126-01
  contract_id       text NOT NULL,                 -- 对应 data_contract.json 的 contracts[].id
  table_name        text NOT NULL,
  field_name        text NOT NULL,
  violation_count   integer NOT NULL DEFAULT 0,
  samples           text,                          -- 头几条单号，供人核对
  owner             text NOT NULL,                 -- 责任方（产线 / 质量 / DBA）
  due_on            date NOT NULL,
  state             text NOT NULL DEFAULT 'open' CHECK (state IN ('open','closed')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  closed_at         timestamptz,
  close_evidence    text,                          -- 关闭证据：复跑时间 + 违规 0 的原话
  CONSTRAINT ck_rm_no CHECK (rm_no ~ '^RM-[0-9]{8}-[0-9]{2}$')
);
COMMENT ON TABLE gov_remediation_order IS '数据整改单：判据红的落地载体，复跑归零才可关闭';

-- 同一契约在同一张表上只允许一张未关闭的单子：重复红不该制造一堆纸
CREATE UNIQUE INDEX IF NOT EXISTS ux_remediation_open
  ON gov_remediation_order (contract_id, table_name) WHERE state = 'open';
