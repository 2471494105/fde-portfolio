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
scripts/bootstrap.ps1      一条命令跑完 001~003，幂等
scripts/export_registry.py 登记册 -> 与 source_schema 同构的 JSON
scripts/query_readonly.py  只读取数 + 越权自证
scripts/export_csv.py      可配字段集/列序/编码的 CSV 导出
api/app.py                 分页接口（FastAPI，8021）
```

**执行顺序**：`bootstrap.ps1` → `004/005`（主数据）→ `006/007`（业务）→ `008`（治理）→ `009`（空值补丁）。
`007` 会清空业务表重灌，跑完必须再跑 `009`，否则那两条 NULL 行没了。

连接串在 `.env`（不入库）。