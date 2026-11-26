# mock-mes

> 模拟"客户现场那台质量管理后端"。不是又一个 demo 服务，是给数据治理判据换一个真实的靶子。

## 为什么单独一个库

`fde_sandbox` 里我们自己说了算，权限松、能随便改。客户现场不是：只读账号、结构在对方手里、
改字段不通知。这些差异只有换一台库才演得出来。

## 三个账号的分工

| 角色 | 能做什么 | 演谁 |
|---|---|---|
| `postgres` | DDL + DML | 客户 DBA |
| `qms_app` | 只 DML；`CREATE` 必须被拒 | 客户业务系统 |
| `ro_reader` | 只 `SELECT` | 开给我们的只读账号 |
| `auditor` | 只看 `aud` schema | 审计员 |

## 目录

```
sql/001_roles.sql      三个账号（集群级）
sql/002_database.sql   建库 fde_client
sql/003_schema.sql     骨架与授权（public / gov / aud 三个 schema）
scripts/bootstrap.ps1  一条命令把这三步跑完，幂等
```

## 起库

```powershell
powershell -File scripts\bootstrap.ps1
```

连接串在 `.env`（不入库）。