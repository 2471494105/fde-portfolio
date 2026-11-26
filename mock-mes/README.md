# mock-mes

> 模拟"客户现场那台质量管理后端"。不是又一个 demo 服务，是给数据治理判据换一个真实的靶子。

## 为什么单独一个库

`fde_sandbox` 里我们自己说了算，权限松、能随便改。客户现场不是：只读账号、结构在对方手里、
改字段不通知。这些差异只有换一台库才演得出来。

## 四个账号的分工

| 角色 | 能做什么 | 演谁 |
|---|---|---|
| `postgres` | DDL + DML | 客户 DBA |
| `qms_app` | 只 DML；`CREATE` 必须被拒；只能写审计、读不了审计 | 客户业务系统 |
| `ro_reader` | 只 `SELECT`，只在 `public` | 开给我们的只读账号 |
| `auditor` | 只看 `aud` schema | 审计员 |

## 三条取数路（同一份数据，三种信号强度）

```powershell
# ① 只读库直连：信号最强（元数据，空列也看得见），但看不到 gov/aud
python scripts\query_readonly.py

# ② CSV 导出：只有表头没有类型；编码、字段集、列序都可变（客户导出就是这样）
python scripts\export_csv.py                                   # utf-8，9 列
python scripts\export_csv.py --encoding gbk --columns equip_no conf_pct rec_id

# ③ 分页接口：信号最弱（键集合随行、随视图变）
powershell -File api\run.ps1
# 起后访问 http://127.0.0.1:8021/v1/defect_records?page=2&size=10&omit_null=1
```

三条路给出的日期文本不一样，这是设计结果不是 bug：库直连 `2026-11-01 08:00:00+08:00`，
本仓导出的 CSV 同形，客户原始 CSV 是 `2026/11/01 08:00`。

## 改结构的正确姿势：变更单 → 登记册 → 快照

```powershell
python scripts\simulate_change.py --req CR-2026-0001 --action apply    # 改结构 + 补登记册
python scripts\export_registry.py                                     # 重导快照，顺手同步给判据
python scripts\simulate_change.py --req CR-2026-0001 --action revert   # 回滚
```

**三处必须一起动**，少一处判据立刻报红（这不是判据的错）：

| 处 | 谁在改 | 判据看不看得到 |
|---|---|---|
| 库里的列 | `simulate_change.py` 的 DDL | 看得到（信息模式） |
| `gov.field_registry` | `simulate_change.py` 的登记 | 看不到，只是底账 |
| `data/mapping/source_schema/*.json` 快照 | `export_registry.py` | **判据读的就是这一份** |

只改了前两处、没重导快照，`check_source_schema` 照样报 `S1 新增列`——这是本项目最有说服力的一条现象：
**"我们改了、判据还说没改"，根因是副本没换，不是判据失灵。**

## 故障注入（跑判据的靶子）

```powershell
python scripts\inject_faults.py --fault f1 --action apply|revert   # 结构先动、登记落后
python scripts\inject_faults.py --fault f2 --action apply|revert   # 登记先动、结构没动（动的是快照）
python scripts\inject_faults.py --fault f3 --action apply|revert   # 值域收紧、存量违约
python scripts\verify_change_flow.py                              # 15 项检查，自动预清理，可重复跑
```

`f3` 是唯一一种**我们想改也改不进去**的变更：库里早有 `conf_pct=150/-10`，加 `CHECK (0..100)` 会被 PG 当场拒。
先洗数据还是先放宽声明是业务要拿主意的事——判据能做的是把它拦在结构变更这一刻。
## 客户端页面（同一进程里的只读视图：作业五页 + 中控台）

```powershell
.\api\run.ps1            # uvicorn api.app:app --port 8021
# 浏览器打开 http://127.0.0.1:8021/app   （不要双击 index.html，file:// 下拉不到接口）
python -X utf8 scripts\verify_ui.py   # 10 组渲染对账，全 PASS
python -X utf8 scripts\verify_obs.py   # 可观测性六段（连库/指标/新鲜度/静默/不写台账）
```

五页和接口是同一个进程：页面上每一个数字，都是 `ro_reader` 从这个库里读出来的，没有第二份假数据。

| 页 | URL | 条数 | 这一页要让你看见什么 |
|---|---|---|---|
| 设备台账 | `/app/#devices` | 48 | 主数据是干净的；`install_dt` 到页面只剩 `2023-01-05`，原始 `2023/01/05` 的写法在库这一层已经丢了 |
| 缺陷记录 | `/app/#defect_records` | 302 | 脏行必须露出来：`conf_pct` 越界（红 2）、`captured_at` 是未来（橙 1）、`lot_no` 空串或 NULL（灰 2，两种"空"分色显示）；KPI `脏行 5 / 302（越界 2 · 未来 1 · 空批次 2）`，与行标同一份 `flag.js` |
| 维修工单 | `/app/#work_orders` | 60 | 两套状态并存，谁也不是权威：红=关单了看板没关（19 张，`已完成` 10 + `已取消` 9），橙=看板关了没关单（6 张），顶部 KPI 直接报 `25 / 60` |
| 数据整改单 | `/app/#remediation_orders` | 2 | 判据红的落点：`RM-20261126-01`（工单 60 行）、`RM-20261126-02`（缺陷 302 行），责任方「产线计划员」、到期 2026-12-03；KPI `未关闭 2 / 2（逾期 0 · 违约 362 行）`。绿=已关闭 / 橙=未关闭 / 红=逾期 |
| 判据运行 | `/app/#check_runs` | 2 | **判据自己有没有在工作**：`gov_check_run` 台账逐次一行（结果 / 红数 / 耗时 / 观测面），KPI 一行报 `运行 4 次 · 最近 1.1 分钟前 · p95 170.6 ms · 4xx/5xx 31（静默判据 0 · 陈旧表 4 · 未来时间 1）`。绿=ok / 黄=warn / 红=fail / 灰=error |
| 数据中控台 | `/console` | 4 条事件 | 深色大屏，**同一进程的第二张皮**：**常见问题目录**（登记 8 类 / 今日命中 4 类，每类带判据、责任方、今日样本、第一步）、事件流（级别 / 类别 / 状态 / 轮次 / 责任方）、违约行环形（362 / 5 / 0，98.6% 集中在工位归属，**只算行类判据**）、各判据红行趋势（五条，不伪造「合计」）。第三张卡报「事件扫描 0 分钟前」——**扫描超 24 h 或从未跑，卡片转红并弹横幅说明跑哪条命令恢复**。只读、无关闭按钮；认领与关闭走 `raise_alerts.py --ack/--close` |

翻页与搜索可以直接写进 URL，录屏和复述都靠它：
`/app/?page=2#work_orders`、`/app/?q=CX-14#devices`（命中 9 台）。

**口径说明**：工单页判红/橙**不靠"state→stage_id 映射表"**。库里的种子是状态×阶段交叉出来的，任何一对一映射都会误判一片；能判的只有"事实跟状态对不上"这两个方向。判定只有一份实现：`api/static/flag.js`（`rowFlag` 行标 / `counts` KPI 计数 / `judge` 按工单号列表），行标与 KPI 都调它，词收在 `MES_WORDS`；`scripts/verify_ui.py` 里是同一条规则的独立实现——**两边对不对得上不再是靠人看**：`fde-diagnosis-agent` 的判据 H 会把同一批工单喂给两侧，按工单号比集合（不比条数，条数相等可能是一换一）。

**整改单页的口径**：`state` 只有 `open` / `closed` 两种，**关闭不是点按钮**——`fde-diagnosis-agent` 的 `scripts/raise_remediation.py --close` 会复跑判据，违规归零才写 `closed` + 关闭证据；还剩 60 / 302 行时它直接拒绝。页面按同一套语义着色，数据全来自 `gov_remediation_order`，没有前端自算。

**验收口径**：`scripts/verify_ui.py` 不用截图判对错——截图只能证明"看起来像"。它拿 Edge 无头渲染后的 DOM，把每行底色、标记、页脚区间、页码、上一页/下一页禁用态逐项与 `/v1/*` 的返回体比对，同时收 stderr 里的 CONSOLE 报错。目前 10 组（五页首页 + 末页 + 第 2 页 + 搜索页）全 PASS，控制台 0 报错，侧栏计数 `48 302 60 2 4`。判据运行页的 KPI 断言取整个 `<span id="kpi">` 段再匹配——早先用 `text()` 取 innerHTML 会在内层 `<b>` 的第一个 `</b>` 处截断，读到 `运行 2` 就没了。

**可观测性三个入口**：`GET /healthz`（真连库 `select 1`，失败 503，不是进程活着就算健康）、`GET /v1/obs/summary`（接口指标 + 新鲜度 + 判据台账一次取全，页面首屏只打这一枪）、`GET /v1/obs/{api,freshness,runs}`（各自单独取）。中间件给每个响应加 `X-Resp-Ms`，并跳过 `/v1/obs*` 自身——读指标不能污染指标。`/v1/obs/api` 另给 `errors_by_path`（不只报错误总数，还要答出哪条接口在坏），并跳过浏览器自己来要的 `/favicon.ico`（它的 404 不是接口故障）。`scripts/verify_obs.py` 的 O1–O6 分段证明：连库 / 计数在涨 / 读指标不进样本 / p95 有值 / 工单表被标陈旧（7497.7 h）/ 缺陷表被标**未来时间**（-849.3 h）/ 主数据无时间列不误报 / 30 小时没跑判静默 / `--no-record` 不写台账。口径写在 `docs/20261126_21_可观测性.md`（`DOC-63`）。

**中控台的口径**：事件不是「每次运行一条」，而是「同一件事只留一条活着」（`dedup_key` + `seen_count`）；同一批红再跑不推、变绿只写「复跑归零」、外发失败写进 `detail.delivery` 且不当作已推。页面只读，不重算判据：未处理事件来自 `/v1/alerts`，违约行与趋势来自 `/v1/obs/runs`，p95/新鲜度来自 `/v1/obs/summary`。趋势线纵轴从 0 起，所以 1 行的变化图上只有约 0.1 px——**不拉伸纵轴**（那会把噪音画成悬崖），改在数字右侧写「较上一次」`−1 / ±0 / +n`。常见问题目录的口径（登记 8 类 ≠ 8 个问题、`red_count` 三种单位、值域黄不写成红、静默样本不取推送数）见 `fde-diagnosis-agent/docs/20261127_26_常见问题目录.md`（`DOC-68`）；页面 `4 / 8 类` 与事件「类别」列都来自 `gov_issue_catalog` 与事件 `detail`，页面不自己拼名字。验收 `scripts/verify_console.py` C1–C10e 全 PASS（含「无任何外网资源」——断网现场也能开），`scripts/verify_alerts.py` A1–A7 全 `[ok]`（在 `fde-diagnosis-agent` 下跑）。口径写在 `docs/20261126_23_中控台与推送.md`（`DOC-65`），「补一行→367 变 366→复原」的彩排见 `docs/20261127_25_收口演示.md`（`DOC-67`）。

**截图证据**：`out/ui/*.png`（1280 与 1024 两档，设备/缺陷/工单/整改单/判据运行各一套，另有 `console-catalog-1280.png`（4 / 8 类、目录 8 行、事件流 4 条带类别、趋势 5 条）与 `console-catalog-1024.png`，以及彩排瞬间的 `console-fix-visible-1280.png`：合计 366、工单 361、趋势 `−1`、事件 #1「已认领」）。

## 目录

```
sql/001_roles.sql          四个账号（集群级）
sql/002_database.sql       建库 fde_client
sql/003_schema.sql         骨架与授权（public / gov / aud）
sql/004_master.sql         主数据六表
sql/005_master_seed.sql    主数据种子 4线/16工位/48设备
sql/006_biz.sql            业务五表
sql/007_biz_seed.sql       业务种子 60/80/300/40/120
sql/008_gov.sql            治理四表 + 从元数据灌登记册
sql/009_null_anomaly.sql   两条"库里形态的空值"（NULL 与空串不是一回事）
sql/010_audit.sql          审计触发器（update 只记变化的列）+ 越权安全阀 gov.read_guarded()
sql/011_station_id_upgrade.sql  工位稳定键 station_id（存量库升级，幂等；新装由 004/005/006 直接建）
sql/012_remediation.sql    数据整改单 gov_remediation_order（同契约同表只许一张 open）
sql/013_observability.sql 判据运行台账 gov_check_run（判据跑没跑、跑了多久、红多少）
sql/014_alerts.sql         中控台事件表 gov_alert（dedup_key / seen_count / open-acked-closed 三态）
sql/015_issue_catalog.sql  常见问题目录 gov_issue_catalog（8 类：客户的说法 / 判据 / 责任方 / 首步 / 口径）
scripts/bootstrap.ps1      一条命令跑完 001~003，幂等
scripts/export_registry.py 登记册 -> 与 source_schema 同构的 JSON
scripts/query_readonly.py  只读取数 + 越权自证
scripts/export_csv.py      可配字段集/列序/编码的 CSV 导出
scripts/verify_audit.py    T7 验收九条断言（用合成探针工单，可反复跑）
scripts/simulate_change.py 按变更单执行/回滚 DDL，只认 approved
scripts/inject_faults.py   三种故障注入（结构先动 / 登记先动 / 值域收紧）
scripts/verify_change_flow.py T8 验收十五项检查，自带预清理
api/app.py                 分页接口（FastAPI，8021）+ 静态挂载 /app
api/static/index.html      客户端五页（单文件 HTML + CSS，无框架）
api/static/console.html    数据中控台（深色，只读；与作业页同进程、同端口）
api/obs.py                 可观测性：接口指标环形缓冲 + 数据新鲜度 + 判据运行台账
api/static/flag.js         红/橙判定唯一的一份实现（行标与 KPI 共用；判据 H 拿它对账）
scripts/verify_ui.py       五页渲染对账（DOM vs 接口，10 组检查）
scripts/verify_console.py  中控台渲染对账（C1–C10e：五组数字 + 环形 + 趋势「较上一次」+ 目录行数与命中数 + 事件类别列 + 违约行不混单位 + 无外链）
scripts/verify_obs.py      可观测性六段验收（O1–O6）
```

**执行顺序**：`bootstrap.ps1` → `004/005`（主数据）→ `006/007`（业务）→ `008`（治理）→ `011`（工位稳定键，存量库升级；新装由 004/005/006 直接建，跑到这里也是空操作）→ `012`（整改单表）→ `009`（空值补丁）→ `010`（审计触发器）。跑故障注入或变更模拟前先 `pg_dump` 一份；改坏了按这个顺序重灌，40 秒左右。
`007` 会清空业务表重灌，跑完必须再跑 `009`，否则那两条 NULL 行没了。

连接串在 `.env`（不入库）。

## 判据侧（在 agent 仓库里）

这个库的价值是"给判据当靶子"，判据本身不在这个目录。`export_registry.py` 导出的快照给 `fde-diagnosis-agent/scripts/check_source_schema.py` 用（S1–S4），`--sync-to` 直接落到 `data/mapping/source_schema/`。

换源验收——把八条判据指到 `fde_client`，看换一家客户时哪几处必须拒跑——跑法与九条反例的档位见 `../fde-diagnosis-agent/docs/20261126_13_换源验收.md`（`DOC-55`）。反例造在 `probe` schema 的同构影子表上，这个库的客户表一行不动。
