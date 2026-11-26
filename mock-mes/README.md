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
api/app.py                 分页接口（FastAPI，8021）
```

**执行顺序**：`bootstrap.ps1` → `004/005`（主数据）→ `006/007`（业务）→ `008`（治理）→ `009`（空值补丁）→ `010`（审计触发器）。跑故障注入或变更模拟前先 `pg_dump` 一份；改坏了按这个顺序重灌，40 秒左右。
`007` 会清空业务表重灌，跑完必须再跑 `009`，否则那两条 NULL 行没了。

连接串在 `.env`（不入库）。