-- 015_issue_catalog.sql · 常见问题目录（T26）
--
-- 判据按「怎么判」组织（I/J、K、A–H、S1–S5），客户按「我这儿出了什么事」组织。
-- 两者不对齐，中控台就会出现上一轮的尴尬：只有两条事件，客户看不出这台库
-- 到底有多少种典型毛病，也看不出「哪些毛病今天没有」——而后者恰恰是他判断
-- 这套系统有没有覆盖他日常的依据。
--
-- 这张表登记的是**类别**，不是运行结果：
--   registered = 这张表的行数（登记了几类）
--   hit        = 各 check_id 最近一次台账 red_count > 0 的类别数（今天命中几类）
-- 两个数在页面上分开报，不许把「登记 8 类」念成「发现了 8 个问题」。
--
-- 内容由 scripts/seed_issues.py 从 app/ds/issues.py 同步（Python 是唯一源），
-- 手工改表会在下一次 seed 被覆盖——想改类别请改 Python 再 seed。

create table if not exists public.gov_issue_catalog (
    issue_id   text primary key,
    sort_no    integer not null,
    short      text    not null,
    name       text    not null,
    check_id   text    not null,
    owner      text,
    first_step text    not null,
    evidence   text    not null,
    note       text,
    updated_at timestamptz not null default now()
);

comment on table public.gov_issue_catalog is '客户侧的常见问题类别：一条 = 一个能被指着讲的现象，判据只是它的检测手段';
comment on column public.gov_issue_catalog.check_id is '用哪道判据的最近一次台账算「今天有没有样本」；一个判据只归一类';
comment on column public.gov_issue_catalog.first_step is '客户能自己做的一步（补哪张表 / 让谁确认 / 看哪张单），不写「加强管理」';
comment on column public.gov_issue_catalog.evidence is '样本怎么数出来的口径，页面不重算';

create index if not exists ix_issue_catalog_sort on public.gov_issue_catalog (sort_no);
