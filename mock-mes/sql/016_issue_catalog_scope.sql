-- 016_issue_catalog_scope.sql
-- 常见问题目录补两列（T27）：把「客户库的问题」与「外部自检」分开，并登记「问题 → 目标数据」的映射。
--
-- 为什么要加：
-- 上一版把 data_contract（跑在 fde_sandbox）与 source_values（跑在 client_a 的 CSV）当成客户库事件接了进来，
-- 客户点进去会发现那条脏行不在自己库里。scope=external 让这两类留在台账、但不进客户事件流。
-- targets 让明细页知道「这条事件的编号该去哪张表、拿哪个键、改哪一栏」，页面因此不必再硬编码表名。
--
-- 幂等：可重复执行（列已存在就不动）。值由 fde-diagnosis-agent/scripts/seed_issues.py 写入，本文件只管结构。

alter table public.gov_issue_catalog add column if not exists scope text not null default 'client';
alter table public.gov_issue_catalog add column if not exists targets jsonb not null default '[]'::jsonb;

comment on column public.gov_issue_catalog.scope is
  'client=跑在 fde_client 上，红了开事件、能点进明细页；external=沙箱库/源端样本，自检留档，不进客户事件流';
comment on column public.gov_issue_catalog.targets is
  '问题 → 目标数据：目标表 / 定位键 / 编号前缀 / 展示列 / 改哪一栏 / 可跳页面；Python 源在 app/ds/issues.py';
