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
| 没做 Agent 轨迹级评测 | 26 条用例（含 3 条多轮）+ 四指标 + 失败归因，出报告 |
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

**评测 — 当前口径**（`eval/report_20261123_0824_c66f83b.json`，`report_eval.json` 是它的副本）

`code_hash=cd9a0a36bce3` / `cases_hash=4c9bd0d53ec4` / `seed_hash=9caa1322fbfc`，26 条全量串行、官方 `deepseek-chat`、temperature=0、mock QMS 在线、基准种子：

```
语义正确率   26/26 = 100%        明确出处率  284/291 = 97.6%
可核验率     291/291 = 100%      引文核对    16 成 / 0 败
有未溯源数字 0/26                失败归因    无
判据解析失败 0/26                终态词豁免  0 引号内 / 0 逐字在证据
总耗时       331 s（12.7 s/条）
```

**别再说 98.4%。** 那是 `report_20261120_1149`（11-20）那轮的数（308/313，claim 总量 313）；这一轮 291。分母不同，比例项不能横比——模型这轮少写了几条多值 claim（多值引用 28 → 23）。能横比的只有语义 26/26、红线 0、引用写错 0、失败归因无。逐项对照见 `eval/报告状态.md`。

三句必须一起说，否则这个 100% 会被读成"稳定满分"：

1. **这是一次 `pass@1`，不是稳定性。** 同一套夹具前六轮在 23–24/26 之间摆动；
   拿到 26/26 之前修掉了三条假红（`terminal_claim`、`cite_wrong`、I9 无单可答）。
2. **`report_20261120_1141_1f5671e.json`（25/26）不能引用**：那次 mock QMS 全程没起，
   W3 那条红是环境——模型自己在结论里写了"工单系统（mock QMS）连接被拒绝"。
3. **两道新闸这轮没开火**：`判据解析失败 0/26`、`终态词豁免 0/0`。所以不能写成"闸修好了 H2"——
   H2 这轮靠的是否定词表，闸的证据目前只有单测的正反两侧。

判断一份历史报告能不能引用，只看两根轴：`cases_hash` 与 `code_hash` 是否与当前相同；
60 多份报告逐份的角色说明在 `eval/报告状态.md`。

### 历史记录（24 条时期，不可当当前基线）

```
语义正确率   22/24 = 91.7%（8 轮里出现 4 次，区间 83.3%–91.7%，均值 90.0%）
工具命中率   0.95      维度覆盖率 0.99
终局命中率   0.88      步数越界 2/192
意图判错     0/192     fc 失败 0/192      未溯源数字 0/192
```

91.7% 不是稳定值，是 8 轮里出现次数最多的那个值。之前报的 95% 是单次快照，同样不能当基线。

当时**稳定失败只有 H5 一条**：用户问"CX-104 尺寸类误判多，是不是阈值太严"，模型只在算法侧取证，没查 `query_calibration`、也没按 day 聚合，缺"什么时候开始变"这一维。**这条已在 2026-11-19 修掉**——修法是在 `query_defect_log` 的工具描述里加两条约束（时间线是必查维度、用户带来的猜测不构成证据），不是放宽判据。修后 H5 的结论原话是"方向不是判定阈值过严，而是标定漂移未被处置"，过程中真调了 `group_by=day,defect_type,station` 和 `query_calibration`（拿到 `drift_mm=0.42` 告警）。经过见 `docs/20261115_05_M4评测.md` 第八节。其余失败项（T1/W1/W3/I2）每轮各出现 1–2 次，属波动。

**意图判错 0/192** 是这轮改造最直接的收益：五类意图（诊断/查数据/查知识/执行动作/追问）一次没判错，而改造前系统里根本没有这个判断——只要句子里有设备号就当诊断。

改造过程、三个自造 regression、判据为什么收窄，写在 `docs/20261116_01_意图层改造.md`。

> 以下多轮与回归数字同为 2026-11-15/16 的改造期记录，用的是当时 24 条夹具，不可与当前 26 条基线横比。

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

- **L1 意图识别**（规则前置 + 模型兜底 + 按代价分档）：三条高精度规则先兜住罕见组合——`guess`（不用查/凭经验）、`act_verb`（建/派/登记 + 工单，要求动宾关系）、`change+dev`（变化词 + 设备号），当前 26 条夹具上命中 7 条、**7/7 与期望一致**（救人 0 / 误伤 0，见基线报告 `rule_hit / rule_saved / rule_harm`）；命中之后不再听模型的 intent（模型照旧要调，因为它要抽 phenomenon / param_names）。其余 19 条交给模型，程序只校验自洽：判成 diagnose/lookup/action 却没给设备号 → 降级追问；没描述现象不算矛盾（可以先取数据），不降级。**不确定时不再一律追问**，而是按 `BRANCH_COST` 分档：`action`（有外部副作用）/ `diagnose`（跑十几步）先问一句，`lookup` / `knowledge`（一步给答案、答偏了眼看得见）照做，并把"我按什么理解的"写在结论最前面。
- **L2 方向判断**（模型判断）：查什么、往硬件还是算法查，全在模型推理里，代码不含数值 if-else
- **L3 证据冲突**（工具驱动）：参数在不同文档里取值不一致时必须上报，不许自己挑一个用。知识分支问的是参数取值时也会自动查一次冲突

程序只做三件模型做不好的事：补信息、执行工具、校验结论。

为什么 L1 不全用规则表：**弱规则会误伤**。`why+dev`（为什么 + 设备）在当时的 27 条夹具上精度 86%、`look_only`（查记录）50%、`param_only`（阈值/亮度）33%——它们依赖的是"记录""阈值""原因"这类常见词，真实用户表达一丰富就会把本来正确的判断改错，而且**这种错在报表上看起来像"模型退化了"**，归因完全跑偏。所以规则只做**前置兜底**（罕见组合、出手必对），不做**全覆盖替代**：覆盖率交给模型，口径交给规则。这既不是 if-else workflow（19/26 仍由模型判），也不是"完全信任模型"。

反过来，"不确定就问"这条也不成立：判据应该是**问的收益 vs 问的成本**（clarify-or-commit，arXiv 2601.06407），还要分清 spec 缺失（用户没说清，该问）和 model uncertainty（用户说清了、模型没把握，**该改提示词而不是问**，ACL 2026 findings）。按这条改完，"模型判错了"不会再被一句追问糊过去变成假绿。

### 执行边界：这是 recommendation，不是 command

这个 demo 不连 PLC，不下发任何控制指令。它能对外改动的只有一件事：往 mock QMS 写一条**待人工确认的工单草稿**。

- **模型输出的是建议，不是指令。** 结论里的 `advice` 是"建议按以下步骤处置"，不是已经执行的动作。建单的门槛很高（根因明确 + 处置明确 + 用户明确要求），而且建完之后还有三道闸（根因 / 证据 / 设备号）和幂等键守着，不会重复建。
- **模型不声称已写入。** 建单成功的唯一依据是**回读**——工具响应被程序读回来校验，不是模型自己说"已创建"。mock QMS 刻意复现了一个真实集成陷阱：建单失败时 HTTP 仍返回 200，错误码在响应体里；只看 HTTP 状态码就会把失败当成功。
- **模型不声称结果。** 不许出现「故障已排除」「已修复」「维修完成」这类终态声明（prompt 第 11 条 + `eval/run_eval.py` 的 `TERMINAL_CLAIM_WORDS`，判据单独成码 `terminal_claim`）。它读过历史记录，但看不到设备此刻的状态，也看不到现场有没有人动手。

一句话：**建议的边界在模型，执行的边界在系统。** 模型可以说"该怎么做"，不可以说"已经做好了"。

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

**当前配置走官方端点**：`.env` 里 `FDE_PROVIDER=deepseek`、`FDE_MODEL=deepseek-chat`，没有设 `FDE_BASE_URL`。
上面的基线 `1149` 就是在这一配置下跑的。

曾经走过中转站（`https://minekuai.cn/v1` / `deepseek-v4.1-flash-1017`），原因只有一个：成本。
代价是峰时排队能到 120 秒以上，同一条问题谷时 24.9 秒、峰时 160.3 秒，差 6.4 倍。
**换端之后 W0 选型与 24 条历史评测的数字都不再可比**——那不是同一个模型，也不该拿新端点的结果覆盖旧数据。

```powershell
# 1. 建库导数据
$env:PGPASSWORD='postgres'
& 'E:\POSTGRESQL\bin\psql.exe' -h localhost -U postgres -d fde_rag -f data\schema.sql
& 'E:\POSTGRESQL\bin\psql.exe' -h localhost -U postgres -d fde_rag -f data\seed\002_data.sql
& venv\Scripts\python.exe data\manual_seed.py          # 手册片段 + 向量，需要 Ollama 在跑

# 2. 端点与模型写进项目根 .env（不要提交）
#    FDE_PROVIDER=deepseek
#    FDE_MODEL=deepseek-chat                    # 基线 1149 用的就是它
#    FDE_BASE_URL=（不写就走官方端点；要用中转站才填 https://...）
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
& venv\Scripts\python.exe eval\run_eval.py               # 全量 26 条
& venv\Scripts\python.exe eval\run_eval.py --workers=3   # 并发（建单类自动串行）
& venv\Scripts\python.exe eval\run_eval.py --only=H5,H3  # 只跑指定 case
```

轨迹按 run 落盘在 `runs\<时间戳>_<id>.jsonl`，一行一个事件，
记录到单次 LLM 调用（prompt/completion token）和单次工具调用（耗时、参数、返回摘要）。

## 怎么验证

改完按改动面选最小验证，别动不动就跑全量 26 条（当前约 333 秒、平均 12.8 秒一条）。

| 命令 | 耗时 | 什么时候跑 | 判据 |
|---|---|---|---|
| `venv\Scripts\python.exe -m unittest tests.test_guardrails` | 0.2 秒 | 动了 `app/graph/*.py`、`app/tools/*.py`、`eval/run_eval.py` | **289 条全绿**；不连库、不发 HTTP |
| `venv\Scripts\python.exe scripts\smoke_tools.py` | 10 秒 | 动了工具层 | 8 个工具都返回数据；建单那步幂等命中同号 |
| `venv\Scripts\python.exe scripts\smoke_qms.py` | 10 秒 | 动了建单链路 | 6 项分两组全过；**会在真库建一张单** |
| `venv\Scripts\python.exe eval\run_eval.py --only=I1` | 5 秒 | 动了 prompt、节点、schema | 新报告 `meta.code_hash` 与上一份不同 = 行为可能变了 |

三个容易搞错的地方：

1. **`smoke_qms.py` 会真建单**，不是只读脚本。当天首次新建，再跑就命中幂等返回同号。
2. **只改 `docs/`、`README`、`app/web/` 这类非行为文件时，上面四条都不用跑，`meta.code_hash` 也不会变**——
   `BEHAVIOR_FILES` 只有 10 个文件（`graph/{nodes,build,state,llm}`、`tools/{schemas,workorder,diagnosis,manual,hints}`、`eval/run_eval.py`）。
   2026-11-20 改 `app/web/server.py` + `static/index.html` 时实测 `code_hash` 仍是 `cd9a0a36bce3`，基线不作废。
3. **但改的是行为文件里的注释，`code_hash` 照样会变**。指纹按整文件算，不看改的是不是逻辑。
   这是刻意的：宁可误报，也不要因为抽取规则失效而漏掉 `schemas.py` 的 enum。
4. 报告文件名带 commit（`report_YYYYMMDD_HHMM_<commit7>.json`），`report_eval.json` 只是最新副本。
   两份报告要可比，必须 `model` / `commit` / `code_hash` / `cases_hash` 四根轴全同；四轴校验没过的报告现在直接不落盘。

### 源端判据与换源验收（S1–S5）

数据治理那条线不碰 `BEHAVIOR_FILES`：改它不动 `code_hash`、不用付 373 秒全量。判据分两层——**结构面** S1 漂移 / S2 未消费（黄）/ S3 破坏 / S4 未声明改名，**值域面** S5 量纲与格式；另有口径登记 A/B/C（未登记直接拒跑，`PolicyNotRegistered`）。三档处置：**红 exit 1 / 拒跑 exit 3 / 黄 exit 0**。

验的不是"判据能不能跑"，是"换一家客户时哪几处会跪"。第二家客户 `client_mes` 已独立登记（走 A，`client_a` 的两份样本未动）：

```powershell
# 换源验收全流程：干净态基线 + 十条反例 + 旧客户回归，自带造景与清理，可连跑
venv\Scripts\python.exe -X utf8 scripts\verify_switch.py

# 只看干净态（不造景）
venv\Scripts\python.exe -X utf8 scripts\check_source_schema.py --client=client_mes --dsn=postgresql://postgres:postgres@localhost:5432/fde_client
venv\Scripts\python.exe -X utf8 scripts\check_source_values.py  --client=client_mes --dsn=postgresql://postgres:postgres@localhost:5432/fde_client

# 旧客户回归：不带参数即走 client_a 的 CSV 样本
venv\Scripts\python.exe -X utf8 scripts\check_source_schema.py
venv\Scripts\python.exe -X utf8 scripts\check_source_values.py
```

判据：`verify_switch.py` 末尾必须是 `RESULT: ALL PASS` 且退出码 0；两个单独脚本 exit 3 = 拒跑（**这不是失败，是设计**）。`--keep` 保留 `probe` schema 与临时登记供排查。

九个缺口（含最像假绿的那条：`limit 200` 不带 `ORDER BY` 跳过脏行）与全部档位在 `docs/20261126_13_换源验收.md`（`DOC-55`）。量纲那一档后来单独改过：判据从看极值改成看中位数——一个 `150` 脏值不许掩盖整列换基准，反过来脏值也不许被当成量纲证据（`DOC-56` 的 e8/e9 是成对的）。

## 目录

```
app/
  graph/     nodes.py（intake/understand/plan/act/verify/lookup/knowledge）、build.py、llm.py、state.py、persistence.py
  tools/     diagnosis.py（1-4）、manual.py（5-6）、workorder.py（7）、schemas.py
  web/       server.py + static/index.html（多轮会话靠 localStorage 的 session_id）
  trace.py   轨迹落盘
data/        schema.sql、seed_generate.py、manual_seed.py、verify_seed.sql
eval/        trajectories.py（26 条，含 3 条多轮）、run_eval.py、report_eval.json
mock_qms/    FastAPI 模拟 QMS（刻意复现"HTTP 200 但业务码在响应体"）
scripts/     run_case / replay / smoke_tools / smoke_qms / smoke_web / smoke_multi_turn / smoke_web_session
             check_ctx / prune_checkpoints / start.ps1 / audit_verify
docs/        工程清单、W0/W1/M3/M4、意图层改造、Checkpointer、流式输出、文档对账、
             校验层引用式溯源（08）、护栏架构设计参考（09，跨项目可搬）
             数据治理线：口径登记（DOC-52）、源端 S1–S4 漂移（DOC-53）、值域 S5（DOC-54）、换源验收（DOC-55）
             00_文档索引.md 是全部 56 篇（`DOC-01`–`DOC-55` + `DATA-01`）的状态表：哪篇能引用、哪篇只是历史，先看它
tests/       test_guardrails.py（护栏三项单测：归属校验 / 建单入参 / 观测隔离）
w0/          W0 模型选型验证（独立于主链路，刻意不套框架）
```

## 已知限制

诚实清单，面试被问就靠这些：

1. **数据是造的**，`random.Random(42)` 确定性生成；相机/控制器型号是真的（Cognex In-Sight 2800 系列、
   Keyence CV-X450 + CA-H200CX、CA-DC21E），告警码与额定寿命是仿真实格式自编，没有抄任何厂商手册原文
2. **26 条样本量小，且当前数字是单次 `pass@1`**：有整份全量基线（26/26、333 s、四轴齐全），
   但同一套夹具历史上在 23–24/26 之间摆动过，所以不能对外说"稳定 100%"。
   稳定性要靠同一份夹具连跑 k 次，现在没做
3. **H5 曾经稳定失败，已于 2026-11-19 修掉**：用户说"是不是阈值太严"，模型当时接受这个前提、
   只在算法侧取证，没查标定（`drift_mm=0.42` 就在返回里）、也没按天看（11-07 是 15.4%，11-08 跳到 63.2%）。
   修法是在 `query_defect_log` 的工具描述里加两条约束（时间线是必查维度、用户带来的猜测不构成证据），
   **不是放宽判据**。改前 8 轮 8 次全失败，改后原话变成"方向不是判定阈值过严，而是标定漂移未被处置"
4. **终局判定靠关键词**，模型换个说法或出现在否定语境里会误判（CX-105 那条已实测到：结论写"不是镜头问题"也算命中"镜头"）
5. **每条只考一件事**，没有组合场景。多轮已进自动评测 3 条（`W1` 两轮、`M1` 三轮、`M2` 两轮），
   覆盖"补设备 / 换设备 / 问到术语再回来"；但五轮以上、含证据裁剪的长链路仍没有自动用例，只有手写 smoke
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
   > 这一段是当时的改法。**`[k]` 编号现在已经不用了**——多轮下模型会按"我第几个查的"
   > 重建一套，跨轮必漂。现行契约是 `工具名{参数指纹}#字段路径`，见 `DOC-20`/`DOC-21`。
9. 未做：docker-compose 一键起环境（本机无 Docker，用 `scripts/start.ps1` 代替）；
   checkpoint 清理只有手动脚本（`prune_checkpoints.py`），没挂定时任务
10. **上下文摘要只有结论开头 80 字**，被裁掉的推理过程没保留，多轮深挖会重复劳动；
    `MAX_CTX_TURNS=2` 是按实测 token 拍的，没做 token 级控制
11. **同一 thread_id 并发不设防**：两个请求同时进同一会话会互相覆盖 checkpoint，LangGraph 不拦。
    Web 端一人一 session 天然隔离，多人共享 session 才会撞
