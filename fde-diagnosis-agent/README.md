# fde-diagnosis-agent

视觉检测设备的故障诊断 Agent。给现场工程师一个自然语言问题（"CX-101 夜班误判率飙升，白班还好一点"），
它会自己决定查哪些数据、判断故障在硬件侧还是算法侧、结论里每个带单位的数字都能追溯到某条工具返回，必要时建工单。

它不是一个固定流程的 workflow：代码里没有任何"如果某工位误判率高就查光源"这类分支，
方向判断完全在模型的推理里。

## 为什么做这个

第一个项目（IQC 检验知识问答 + 检验单 Agent）被评为"够得上能把 Agent 上线的工程型候选人，
够不上把 Agent 做聪明的算法型候选人"，三个具体短板：

| 短板 | 这个项目怎么补 |
|---|---|
| 串 5 个固定工具 → 会被问"你这算 Agent 还是 workflow" | L2 方向判断交给模型，代码零数值分支 |
| 没做 Agent 轨迹级评测 | 25 条 golden path + 四指标 + 归因，出报告 |
| 没做模型选型 / function calling 失败率 | W0 双模型对照实测，有数字 |

## 实测数据

**W0 模型选型**（同轨迹同判据，各 15 次）

| 指标 | qwen2.5:7b（本地） | deepseek-chat（API） |
|---|---|---|
| 语义正确率 | 100%（低标准满分，平均 0.9 步） | 73.3%（结论对但证据链不全） |
| fc 解析失败 | 0/23 | 0/81 |
| 平均步数 | 0.9 | 1.5 |
| 单次耗时 | 9.3 s | 6.6 s |

选 DeepSeek 当主力：取证多、更快、失败可发现。7b 留作离线兜底（只保证断网能演示，是降级模式）。
**两个模型都必须带护栏**——DeepSeek 同样在用户没给设备号时编了一个，比例 3/15 与 7b 完全一样，
说明"缺关键参数时模型倾向自己补"是普遍行为，不是小模型专属。

**评测**（当时 24 条，含 4 条意图用例，deepseek-chat，temperature=0，连跑 8 轮。现有 25 条，新增 I9；下表是 24 条时期的 8 轮结果，保留原值不做覆盖）

```
语义正确率   22/24 = 91.7%（8 轮里出现 4 次，区间 83.3%–91.7%，均值 90.0%）
工具命中率   0.95      维度覆盖率 0.99
终局命中率   0.88      步数越界 2/192
意图判错     0/192     fc 失败 0/192      未溯源数字 0/192
```

91.7% 不是稳定值，是 8 轮里出现次数最多的那个值。之前报的 95% 是单次快照，同样不能当基线。

8 轮里**稳定失败只有 H5 一条**：用户问"CX-104 尺寸类误判多，是不是阈值太严"，模型只在算法侧取证，没查 `query_calibration`、也没按 day 聚合，缺"什么时候开始变"这一维。这条故意保留为失败——它是「用户带假设来、系统顺着说」的活样本。其余失败项（T1/W1/W3/I2）每轮各出现 1–2 次，属波动。

**意图判错 0/192** 是这轮改造最直接的收益：五类意图（诊断/查数据/查知识/执行动作/追问）一次没判错，而改造前系统里根本没有这个判断——只要句子里有设备号就当诊断。

改造过程、三个自造 regression、判据为什么收窄，写在 `docs/20261116_01_意图层改造.md`。

**多轮会话（Checkpointer 存 PostgreSQL）**

改造前追问即死：系统在一条追问里给了两条互斥指令（"回一句看数据" / "请给设备编号"），用户照做两条都走不通——根因是 `ask_user → END` 之后 state 无处可存。

改造后同一 `thread_id` 三轮走通：`帮我看看最近怎么样` → `CX-101` → `看数据`，第三轮判 lookup、2 步、直接出数据表。换设备（`那 CX-102 呢`）会切 `slots` 并清掉上一台的 phenomenon。跨进程实测也过：新起 Python 进程复用旧 thread，读回 `turn=2` 和 `slots=CX-102`。

上一层回归：**23/24 = 95.8%**（唯一失败仍是 H5）。首次跑只有 20/24，两条原因都是排查出来的，不是猜的——一条是 mock QMS 没起（模型自己写了"连接被拒绝"），一条是提示词第 7 条让模型过度保守、连用户明确要求的建单也拒绝，补第 8 条区分"指出不一致"与"拒绝执行"后恢复。代价是 checkpoint 写库让单条耗时从 5.5 s 涨到 5.9 s。

上下文管理（裁剪规则、验证据按轮过滤、摘要为什么用规则拼）写在 `docs/20261116_02_Checkpointer持久化与上下文管理.md`。

## 架构

```
START → intake（正则抽候选设备号，不判意图）
      → understand（模型判意图）
          ├─ diagnose / action → plan ⇄ act → verify → finalize → END
          ├─ lookup            → lookup（模板出数据表）──────────┘
          ├─ knowledge         → knowledge（检索手册 + 查参数冲突）┘
          └─ clarify           → ask_user → END

每一轮：state 存 PostgreSQL（LangGraph Checkpointer），同一个 thread_id 再 invoke 就从上一轮恢复。
```

三层岔口：

- **L1 意图识别**（模型判断，程序只校验自洽）：用户想干什么由模型判，程序不写规则表，只校验模型输出有没有硬矛盾——判成 diagnose/lookup/action 却没给设备号，就降级成追问。没描述现象不算矛盾（可以先取数据），所以不降级。
- **L2 方向判断**（模型判断）：查什么、往硬件还是算法查，全在模型推理里，代码不含数值 if-else
- **L3 证据冲突**（工具驱动）：参数在不同文档里取值不一致时必须上报，不许自己挑一个用。知识分支问的是参数取值时也会自动查一次冲突

程序只做三件模型做不好的事：补信息、执行工具、校验结论。

为什么 L1 不用规则表：规则表做意图判断等于把 L1 变成 if-else，整个项目退回 workflow——这正是第一个项目被质疑的点。而意图分类是独立轻调用、单一任务、输出枚举+实体，属于模型擅长的分类任务；W0 里"提示词加约束反而更差"的结论属于另一种情况（同一次推理里塞多条硬约束导致指令过载），不适用于这里。

## 八个工具

| 工具 | 干什么 |
|---|---|
| `query_device_profile` | 设备档案：产线、工位、相机、镜头、光源型号 |
| `query_defect_log` | 误判日志聚合，`group_by` 支持 `hour / day / station / defect_type / batch` |
| `query_calibration` | 标定记录与漂移量，判断是否超阈值 |
| `query_light_usage` | 光源累计点亮时长、亮度调整历史 |
| `search_manual` | 设备手册与厂内 SOP 检索（768 维向量，走本地 nomic-embed-text） |
| `check_doc_conflict` | 跨文档参数一致性核查，按 `params` JSONB 精确比对 |
| `query_work_order` | 查历史工单（只读），建单前先看有没有重复 |
| `create_work_order` | 建单，走 mock QMS 的 HTTP 接口，带幂等 |

取值要求写进工具 description，不写进提示词——这是 W0 实测出来的：加两条硬约束后语义正确率反而从 40% 掉到 20%。

## 三个值得说的工程点

**1. 幂等键必须基于枚举，不能基于自然语言**

第一版用 `hash(device_id + day + root_cause)`，`root_cause` 是自由文本。
模型第一次写"光源超额定寿命导致亮度衰减"、第二次写"光源超寿命"——同一件事，hash 不同，
于是重复建单。改成基于 `root_cause_code` 枚举（`light_degradation` / `lens_contamination` /
`calibration_drift` / `batch_drift` / `threshold_misconfig` / `other`），自由文本只留给工单内容用。

**2. 校验层的误报比漏报更伤**

结论里的百分比要和工具返回比对。规则踩了三次：
- `-?\d+` 会把 `"10%-25%"` 里的 `-25` 当负数抓走 → 工具返回过的 `25%` 被判编造
- 容差 0.002 太紧，`0.6923 → "69%"`（差 0.0023）被判编造
- 模型自己算的 `20/27 = 74%` 被判编造，但算法是对的

一旦频繁误报，人就会忽略告警，校验层等于失效。所以容差放宽到 1 个百分点，
并加了"可复算"判定：能用工具返回里的两个数算出来就放行。

**3. 不合格工具描述会让模型把"现场被调坏的值"当标准**

`search_manual("光源亮度设定")` 首条命中的是厂内 SOP 的 **65**（score 0.8519），
厂商手册的 **80** 排在第二（0.8168）。65 正是 CX-101 已经被调坏的值。
只读第一条就推理，结论会直接错。所以检索命中多源文档时必须显式查冲突，
这条写进了 L3 的触发条件。

## 怎么跑

前置：PostgreSQL（本例 18.6 + pgvector 0.8.7）+ Ollama（`nomic-embed-text`，只在灌手册和检索向量时用）+ 一个 OpenAI 兼容端点（官方 DeepSeek 或中转站均可）。

**当前 demo 配置走中转站**（`https://minekuai.cn/v1`，模型 `deepseek-v4.1-flash-1017`），原因只有一个：成本。
代价是峰时排队能到 120 秒以上，同一条问题谷时 24.9 秒、峰时 160.3 秒，差 6.4 倍。
注意上面 W0 选型和 24 条评测的数字是在**官方 `deepseek-chat` 端点**下测的，换端后不保证复现，也不该拿新端点的结果去覆盖那批历史数据。

```powershell
# 1. 建库导数据
$env:PGPASSWORD='postgres'
& 'E:\POSTGRESQL\bin\psql.exe' -h localhost -U postgres -d fde_rag -f data\schema.sql
& 'E:\POSTGRESQL\bin\psql.exe' -h localhost -U postgres -d fde_rag -f data\seed\002_data.sql
& venv\Scripts\python.exe data\manual_seed.py          # 手册片段 + 向量，需要 Ollama 在跑

# 2. 端点与模型写进项目根 .env（不要提交）
#    FDE_PROVIDER=openai
#    FDE_BASE_URL=https://minekuai.cn/v1        # 换成官方端点亦可
#    FDE_MODEL=deepseek-v4.1-flash-1017
#    FDE_API_KEY=<你的 key>

# 3. 起服务
& venv\Scripts\python.exe -m uvicorn mock_qms.server:app --port 8010
& venv\Scripts\python.exe -m uvicorn app.web.server:app --port 8020
# 浏览器打开 http://127.0.0.1:8020
```

命令行诊断与评测：

```powershell
& venv\Scripts\python.exe scripts\run_case.py "CX-101 误判率有点高，你看看怎么回事"
& venv\Scripts\python.exe scripts\replay.py              # 回放最近一次运行
& venv\Scripts\python.exe eval\run_eval.py               # 全量 25 条
& venv\Scripts\python.exe eval\run_eval.py --workers=3   # 并发（建单类自动串行）
& venv\Scripts\python.exe eval\run_eval.py --only=W1,W2  # 只跑指定 case
```

轨迹按 run 落盘在 `runs\<时间戳>_<id>.jsonl`，一行一个事件，
记录到单次 LLM 调用（prompt/completion token）和单次工具调用（耗时、参数、返回摘要）。

## 目录

```
app/
  graph/     nodes.py（intake/understand/plan/act/verify/lookup/knowledge）、build.py、llm.py、state.py、persistence.py
  tools/     diagnosis.py（1-4）、manual.py（5-6）、workorder.py（7）、schemas.py
  web/       server.py + static/index.html（多轮会话靠 localStorage 的 session_id）
  trace.py   轨迹落盘
data/        schema.sql、seed_generate.py、manual_seed.py、verify_seed.sql
eval/        trajectories.py（25 条）、run_eval.py、report_eval.json
mock_qms/    FastAPI 模拟 QMS（刻意复现"HTTP 200 但业务码在响应体"）
scripts/     run_case / replay / smoke_tools / smoke_qms / smoke_web / smoke_multi_turn / smoke_web_session
             check_ctx / prune_checkpoints / start.ps1 / audit_verify
docs/        工程清单、W0/W1/M3/M4、意图层改造、Checkpointer、流式输出、文档对账、
             校验层引用式溯源（08）、护栏架构设计参考（09，跨项目可搬）
tests/       test_guardrails.py（护栏三项单测：归属校验 / 建单入参 / 观测隔离）
w0/          W0 模型选型验证（独立于主链路，刻意不套框架）
```

## 已知限制

诚实清单，面试被问就靠这些：

1. **数据是造的**，`random.Random(42)` 确定性生成；相机/控制器型号是真的（Cognex In-Sight 2800 系列、
   Keyence CV-X450 + CA-H200CX、CA-DC21E），告警码与额定寿命是仿真实格式自编，没有抄任何厂商手册原文
2. **25 条样本量小**，且**当前只跑通 7 条可信基线**（H1–H5、A1、A2，0 失败，平均 142 s/条），
   其余 18 条（A3–A5、I1–I9、W1–W3、T1–T3）待谷时补跑；补齐前不能对外说"25/25 通过"
3. **H5 那条失败未修复**：用户说"是不是阈值太严"，模型接受这个前提只在算法侧取证，
   没查标定（`drift_mm=0.42` 就在那里）、没按天看（11-07 是 15.4%，11-08 跳到 63.2%）。
   这反映的是"用户断言污染诊断"的真实能力缺口，故意保留为失败。8 轮里它 8 次全失败，不是偶发
4. **终局判定靠关键词**，模型换个说法或出现在否定语境里会误判（CX-105 那条已实测到：结论写"不是镜头问题"也算命中"镜头"）
5. **每条只考一件事**，没有组合场景；跨轮能力有了真实会话（`thread_id`），但**评测还是 25 条单轮**——
   多轮没有自动评测，只有 `scripts/smoke_multi_turn.py` 三个手写 case 覆盖了"补设备 / 换设备 / 摘要累积"
6. 每日样本仅 10–20 条，按日聚合波动大，看趋势要用 3 天以上窗口；5 台设备都有问题，没有健康对照机
7. ~~**告警码没有进向量库**~~ 已修：新增「告警码对照表 ALM-2026」9 块，`device_manual_chunks` 24 → 33 块。
   现在问"E-101 这个告警什么意思"，`search_manual` 首条命中即"E-101 相机未连接或通讯中断 / 告警码 E-101，等级 critical /
   常见原因：网线松脱、相机掉电"，结论能直接答。
   （`manual_seed.py` 是 `TRUNCATE` 后全量重灌，改它必须重跑，否则库里还是旧块。）
8. ~~**校验层实测 0% 拦截**~~ 已修（见 `docs/20261116_08_校验层引用式溯源与流式轨迹.md`）：
   旧逻辑随机编造 300 个百分比，177 个直接命中、123 个靠 `_recomputable` 全池凑数、拦下 0 个；
   且 `PCT_RE` 只匹配 `%`，绝对量（"累计 9200 小时""0.88mm"）完全不校验，所以"未溯源数字 0/24"
   其实是"一个数都没检查"。
   现在改成引用式溯源：结论里每个带单位的量标 `[k]`，校验层逐条比对。审计（`scripts/audit_verify.py`，
   8 条真实证据共 441 个数字）实测：编造百分比拦截率 **0% → 24% → 100%**（旧口径 → 弱溯源 → 强溯源），
   编造绝对量从"根本不校验"变成 **0% 放行**，真实值误报 0/300，标错角 152/152 全拦。
   端到端 CX-101：1485 字结论里 28 个数量、25 个带角且全部可溯源、0 拦截；
   未标角的 3 个退回全证据宽口径，界面单列、不计入引用率。
   （可靠来源：直接命中该条证据，或写出算式 `74%（=20/27）` 且分子分母都在该条证据里；
   靠两数相除凑出来的记 `recomputed`，放行但不算引用。）
9. 未做：docker-compose 一键起环境（本机无 Docker，用 `scripts/start.ps1` 代替）；
   checkpoint 清理只有手动脚本（`prune_checkpoints.py`），没挂定时任务
10. **上下文摘要只有结论开头 80 字**，被裁掉的推理过程没保留，多轮深挖会重复劳动；
    `MAX_CTX_TURNS=2` 是按实测 token 拍的，没做 token 级控制
11. **同一 thread_id 并发不设防**：两个请求同时进同一会话会互相覆盖 checkpoint，LangGraph 不拦。
    Web 端一人一 session 天然隔离，多人共享 session 才会撞
