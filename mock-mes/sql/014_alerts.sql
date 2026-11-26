-- 014_alerts.sql · 中控台的事件表（T23）
--
-- 为什么不是「每次判据红就写一行」：那样同一批 362 行每天刷一条，
-- 三周后没人再看——刷屏和没有告警是同一个结果。所以这里存的是**事件**：
--   dedup_key 同一件事只留一条活着的（open / acked），关掉（closed）之后
--   同一个 key 再次出现才会新增一条，于是「问题复发」本身也是可数的。
--
-- 状态机只有三态，且都是脚本写、页面只读（与整改单同一口径：纸面不能改数据）：
--   open   → 判据红（或判据静默 / 单子逾期）
--   acked  → 有人认领（写 by + at）
--   closed → **复跑归零**才允许（写 closed_reason）；手工 close 只能写原因，不能写「修好了」

create table if not exists public.gov_alert (
    alert_id      bigserial primary key,
    dedup_key     text        not null,
    check_id      text        not null,
    kind          text        not null check (kind in ('check_red', 'check_silent', 'order_overdue')),
    severity      text        not null check (severity in ('red', 'amber')),
    title         text        not null,
    detail        jsonb       not null default '{}'::jsonb,
    owner         text,
    first_seen    timestamptz not null default now(),
    last_seen     timestamptz not null default now(),
    seen_count    integer     not null default 1,
    state         text        not null default 'open' check (state in ('open', 'acked', 'closed')),
    acked_by      text,
    acked_at      timestamptz,
    closed_at     timestamptz,
    closed_reason text
);

comment on table public.gov_alert is '中控台事件：一条 = 一件需要人处理的活；同一 dedup_key 同时只许一条活着';
comment on column public.gov_alert.dedup_key is '同一件事的身份（判据 + 类型 + 责任方），不是标题的哈希';
comment on column public.gov_alert.seen_count is '仍在红的次数：涨说明没修，不涨说明没复跑——两件事分开看';
comment on column public.gov_alert.closed_reason is 'closed 只能由复跑归零或人工写明原因，不能写「已修复」';

-- 同一个 key 同时只允许一条活着：open 或 acked 都算活着
create unique index if not exists ux_alert_alive
    on public.gov_alert (dedup_key) where state <> 'closed';

-- 页面按最近活动倒序翻
create index if not exists ix_alert_recent on public.gov_alert (last_seen desc);
create index if not exists ix_alert_open on public.gov_alert (severity, state);
