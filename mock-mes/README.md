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
## 客户端三页（同一进程里的只读视图）

```powershell
.\api\run.ps1            # uvicorn api.app:app --port 8021
# 浏览器打开 http://127.0.0.1:8021/app   （不要双击 index.html，file:// 下拉不到接口）
python -X utf8 scripts\verify_ui.py   # 8 组渲染对账，全 PASS
```

三页和接口是同一个进程：页面上每一个数字，都是 `ro_reader` 从这个库里读出来的，没有第二份假数据。

| 页 | URL | 条数 | 这一页要让你看见什么 |
|---|---|---|---|
| 设备台账 | `/app/#devices` | 48 | 主数据是干净的；`install_dt` 到页面只剩 `2023-01-05`，原始 `2023/01/05` 的写法在库这一层已经丢了 |
| 缺陷记录 | `/app/#defect_records` | 302 | 脏行必须露出来：`conf_pct` 越界（红）、`captured_at` 是未来（橙）、`lot_no` 空串或 NULL（灰，两种"空"分色显示） |
| 维修工单 | `/app/#work_orders` | 60 | 两套状态并存，谁也不是权威：红=关单了看板没关（10 张），橙=看板关了没关单（6 张），顶部 KPI 直接报 `16 / 60` |

翻页与搜索可以直接写进 URL，录屏和复述都靠它：
`/app/?page=2#work_orders`、`/app/?q=CX-14#devices`（命中 9 台）。

**口径说明**：工单页判红/橙**不靠"state→stage_id 映射表"**。库里的种子是状态×阶段交叉出来的，任何一对一映射都会误判一片；能判的只有"事实跟状态对不上"这两个方向，规则写在 `api/static/index.html` 的 `flag` 里，验收脚本里是同一条规则的独立实现，两边对得上才算过。

**验收口径**：`scripts/verify_ui.py` 不用截图判对错——截图只能证明"看起来像"。它拿 Edge 无头渲染后的 DOM，把每行底色、标记、页脚区间、页码、上一页/下一页禁用态逐项与 `/v1/*` 的返回体比对，同时收 stderr 里的 CONSOLE 报错。目前 8 组（三页首页 + 末页 + 第 2 页 + 搜索页）全 PASS，控制台 0 报错。

截图证据：`out/ui/*.png`（1280 与 1024 两档各三张）。

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
scripts/bootstrap.ps1      一条命令跑完 001~003，幂等
scripts/export_registry.py 登记册 -> 与 source_schema 同构的 JSON
scripts/query_readonly.py  只读取数 + 越权自证
scripts/export_csv.py      可配字段集/列序/编码的 CSV 导出
scripts/verify_audit.py    T7 验收九条断言（用合成探针工单，可反复跑）
scripts/simulate_change.py 按变更单执行/回滚 DDL，只认 approved
scripts/inject_faults.py   三种故障注入（结构先动 / 登记先动 / 值域收紧）
scripts/verify_change_flow.py T8 验收十五项检查，自带预清理
api/app.py                 分页接口（FastAPI，8021）+ 静态挂载 /app
api/static/index.html      客户端三页（单文件 HTML + CSS，无框架）
scripts/verify_ui.py       三页渲染对账（DOM vs 接口，8 组检查）
```

**执行顺序**：`bootstrap.ps1` → `004/005`（主数据）→ `006/007`（业务）→ `008`（治理）→ `009`（空值补丁）→ `010`（审计触发器）。跑故障注入或变更模拟前先 `pg_dump` 一份；改坏了按这个顺序重灌，40 秒左右。
`007` 会清空业务表重灌，跑完必须再跑 `009`，否则那两条 NULL 行没了。

连接串在 `.env`（不入库）。