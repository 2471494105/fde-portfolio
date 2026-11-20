# 视觉检测设备故障诊断 Agent

现场工程师说一句「CX-101 夜班误判率飙升，白班还好一点」，Agent 自己决定查哪些数据、判断故障在光源/标定侧还是算法侧，结论里每个带单位的数字都能追到某条工具返回，必要时建一张**待人工确认**的工单草稿。

它不连 PLC、不下发控制指令。模型输出的是建议，执行的边界在系统里。

## 核心数字

2026-11-20 基线（`eval/report_20261120_1149_1f5671e.json`，26 条用例单次全量串行，官方 deepseek-chat，temperature=0）

| 指标 | 结果 |
|---|---|
| 语义正确率 | 26/26（pass@1） |
| 明确出处率 | 308/313 = 98.4% |
| 可核验率 | 309/313 = 98.7% |
| 引文逐字核对 | 16 处命中 / 0 处不符 |
| 单次全量耗时 | 333 s，平均 12.8 s/条 |
| 护栏单测 | 289 条全绿，0.2 s |
| 四轴指纹 | code_hash `cd9a0a36bce3` / cases_hash `4c9bd0d53ec4` / seed_hash `9caa1322fbfc` / commit `1f5671e` |

口径说明：这是**单次 pass@1**，不是稳定性。同一套夹具历史上在 23–24/26 之间摆动过，所以这里不写「稳定 100%」。判断一份历史报告能不能引用，只看 `cases_hash` 与 `code_hash` 两根轴是否相同。

## 30 秒看懂

1. **这是 Agent，不是 workflow。** 代码里没有任何「某工位误判率高就查光源」这类分支，查什么、往硬件还是算法查，全在模型推理里。程序只做三件模型做不好的事：补信息、执行工具、校验结论。规则只做罕见组合的前置兜底（26 条夹具命中 7 条，7/7 与期望一致，误伤 0）。
2. **结论可核。** 每个带单位的量按 `工具名{参数指纹}#字段路径` 标出处，校验层逐条比对工具返回；能力是分三次迭代出来的——旧逻辑对绝对量根本不校验，审计显示「编造百分比拦截率 0% → 24% → 100%，真实值误报 0/300，标错角标 152/152 全拦」。
3. **边界写在代码里，不是提示词里。** 12 步工具预算、写操作三道闸（根因明确 / 证据齐 / 设备号齐）、基于 6 个 `root_cause_code` 枚举的幂等键；建单成败只认回读——模拟 QMS 刻意复现了「HTTP 200 但业务码藏在响应体」这个真实集成陷阱。

## 目录

```
fde-diagnosis-agent/
  app/graph/         intake → understand → plan ⇄ act → verify → finalize 的状态图
  app/tools/         8 个工具（7 读 1 写）
  app/web/           FastAPI + 静态页，多轮会话靠 thread_id
  eval/              26 条用例（硬件5/算法5/信息不全4/建单2/陷阱3/意图5/多轮2）
  mock_qms/          模拟 QMS，刻意复现集成陷阱
  docs/              工程清单、W0 选型、意图层改造、Checkpointer、溯源校验、护栏设计
  tests/             289 条护栏单测（不连库、不发 HTTP）
```

## 10 分钟跑起来

前置：PostgreSQL（18.6 + pgvector 0.8.7）、Ollama（`nomic-embed-text`，只在灌手册和检索时用）、一个 OpenAI 兼容端点。

```powershell
$env:PGPASSWORD='postgres'
& 'psql.exe' -h localhost -U postgres -d fde_rag -f data\schema.sql
& 'psql.exe' -h localhost -U postgres -d fde_rag -f data\seed\002_data.sql
& venv\Scripts\python.exe data\manual_seed.py

# 项目根 .env：FDE_PROVIDER=deepseek / FDE_MODEL=deepseek-chat / FDE_API_KEY=<你的 key>
& venv\Scripts\python.exe -m uvicorn mock_qms.server:app --port 8010
& venv\Scripts\python.exe -m uvicorn app.web.server:app --port 8020

& venv\Scripts\python.exe scripts\run_case.py "CX-101 误判率有点高，你看看怎么回事"
& venv\Scripts\python.exe -m unittest tests.test_guardrails    # 289 条，0.2 秒
```

`docs/20261120_项目介绍.pdf` 有 11 页完整拆解：场景、输出、轨迹、架构、工程实现、指标、局限、复现、术语。

## 已知边界

- 数据是造的（`random.Random(42)` 确定性生成；相机与控制器型号真实，告警码按真实格式自编）。
- 26 条样本量小，且只做了单次 pass@1，没有跑 k 次稳定性。
- 终局终态判定靠关键词，模型换个说法或在否定语境里会误判。
- 未做 docker-compose 一键起环境；checkpoint 清理只有手动脚本。
- 同一 `thread_id` 并发不设防，两人共享会话会互相覆盖 checkpoint。

完整清单在 `fde-diagnosis-agent/README.md` 的「已知限制」一节，共 11 条。

## 关于我

Java 后端 + 西门子 PLC 现场调试出身，现在做工业场景的 AI 交付。这个仓库是给面试官看的证据，不是简历附属品——所有数字都能在 `eval/` 的报告里复现。
