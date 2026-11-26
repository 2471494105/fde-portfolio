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

数据治理那条线不碰 `BEHAVIOR_FILES`：改它不动 `code_hash`、不用付 373 秒全量。判据分两层——**结构面** S1 漂移 / S2 未消费（黄）/ S3 破坏 / S4 未声明改名，**值域面** S5 量纲与格式；另有口径登记 A/B/C/D/E（A 索引谓词漂移；B 库里出现未登记状态；C 未登记客户拒跑 `PolicyNotRegistered`；D 折法完整性——每个客户词必须折到我方基准五词；E 不适用声明——`unsupported_checks` 的 id 要登记、且真跑一遍证明「该不适用时真的没跑」）。三档处置：**红 exit 1 / 拒跑 exit 3 / 黄 exit 0**。

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

再往后是**换客户时真正会出事的那一层：折法**。`closed_states` 只说了「哪些词算闭环」，没说「折成我方哪一态」——折法缺了，保护集（有未闭环单、不许删的设备）会静默失真，而且不报错。`client_b_odoo` 已登记：闭环 `done`/`cancel`、五条 `vocab_fold`、缺口 `已合并` 显式认领、`work_order_state_stage_conflict` 声明不适用（Odoo 的 `stage_id` 是看板列，不是终态）。

「声明不适用」本身也要能证伪：`unsupported_checks` 现在是 `{check_id: 理由}`，判据 id 登记在顶层 `checks` 里（what / reads / 落点 / 外部对照），跑判据只走 `app/ds/checks.py::run_check` ——它先过 `check_applies()`，声明不适用的直接返回 `not_applicable` 且不读数据，返回值里的 `ran` 是「实现有没有被调用」的诚实信号。`client_b` 声明了看板判据不适用后，仍有一条 `status_vocabulary_coverage`（只管「这个词我认识吗」）在跑，避免「声明把整家客户的判据关空」。
但这套东西的完备性架在三个隐含前提上，每一个都能单独塌。**入口**：E 只看得见走 `run_check` 的调用，有人直接 `import` 实现就绕过 `check_applies()` —— 所以 `bypass_problems(root)` 用 AST 扫全仓库，报 `from app.ds.checks import <实现名>` / 直接调用实现名 / 访问 `_RUNNERS`，解析失败的 `.py` 也报（扫不了不等于没绕过）。**词表归属**：看板关闭词属于客户的看板列，不属于判据 —— `client_a.stage_board_closed` 按客户登记，缺词抛 `CheckNotConfigured`（空词表会让 `stage not in []` 恒真，判出一批假红）。**字段面**：`state` 由 `vocab_fold` 管、`stage_id` 由判据词表管，剩下的 `priority` / `maintenance_team_id` 没人管就会按字面比 —— 数字比数字永远「能跑」，只是判错。按一手样本（`07_外部样本/odoo-maintenance-model.py` 第 291/307 行）分别处理：`priority` 四档同构做恒等折法（不声称标签语义等价），`maintenance_team_id` 是 `Many2one` 的整数 id、我方是文本 `TEAM-n`，没有对方团队字典就映射不了 → 进 `unsupported_columns` 并写理由。
判据从 A–E 扩到 **A–H**：F 扫入口绕过，G 查 `judged_fields`（未覆盖 / 空折法 / 声明无理由 / 折法键不在表里），H 把 `counterpart` 变成对账。证据在 `docs/20261126_17_判据的边界.md`（`DOC-59`）；`DOC-58` 的第 1、3 条未决到此解掉。

最后一块是**同一条规则写了四遍**：页面行标、页面 KPI、`verify_ui.py` 的期望值、治理判据各一份，词都硬写。前两份已收进 `mock-mes/api/static/flag.js`（页面两处都调它），`counterpart` 从一句文本变成结构（`file` / `entries` / `words_client` / `extra_rules`），判据 H 做四件事：入口存在、词与登记逐字对齐、期望值不许自造词、**同一批行喂两侧按工单号比集合**（不比条数——19 对 19 可能是一换一）。`node` 缺了报「跑不了就是没验」，不当通过。**H 第一次跑就红了且红得对**：页面只认「已完成」，判据认「已完成+已取消」，真数据里 `已取消`且看板未关有 9 张——页面少标 9 行红、KPI 少算 9；词对齐后两侧 red 都是 19 且工单号集合相同（KPI「两套状态对不上 25 / 60（红 19 · 橙 6）」）。页面判橙的「关板未关单」6 张 Python 侧没有判据，登记进 `extra_rules` 并在验收里钉死这 6 张确实存在。证据在 `docs/20261126_18_同一条规则写了四遍.md`（`DOC-60`）。

```powershell
# 折法验收：同一批 60 张工单换成 Odoo 词表，两侧保护集必须完全相同；再故意折错，看结论会不会按预期变
venv\Scripts\python.exe -X utf8 scripts\verify_k5_fold.py

# 不适用声明 + 入口绕过 + 词表归属 + 字段面：19 条红 / ran=False / 假入口被抓 / 换词表 19→15 / 折法覆盖真库
venv\Scripts\python.exe -X utf8 scripts\verify_unsupported.py

# 八条口径判据（A 索引 / B 未登记词 / C 拒跑 / D 折法 / E 不适用声明 / F 入口绕过 / G 字段面 / H counterpart 对账）；
# 换客户时把观测面一起换过去，否则查的是自己家的库
venv\Scripts\python.exe -X utf8 scripts\check_status_policy.py
venv\Scripts\python.exe -X utf8 scripts\check_status_policy.py --dsn=postgresql://postgres:postgres@localhost:5432/fde_client --table=probe.wo_b
```

`verify_k5_fold.py` 判据：末尾 `RESULT: ALL PASS` 且 exit 0，跑完 `probe` schema 必须已清。反例是**成对**的：只认 `cancel`（漏折 `done`）保护集 24 → 33 台，只认 `done`（漏折 `cancel`）24 → 41 台——两个方向多出来的不是同一批设备。实测与代价在 `docs/20261126_15_换客户折法验收.md`（`DOC-57`）。

`verify_unsupported.py` 判据同样是 `RESULT: ALL PASS`。三条硬证据：同一批 60 行，`client_a` 适用 → `ran=True`、红 19 条；`client_b` 声明不适用 → `ran=False`、`checked=0`；**只把声明去掉 → 立刻红 19 条**（「不适用」挡的就是每天 19 条假红）。再往后是 H 的九条：真数据两侧 red 19 = 19 且工单号集合相同、页面 6 张橙登记为差异、spec 词改回单值两条静层报、`indexOf < 0` 改 `>= 0` 动层报并列出两侧各多的号、counterpart 改回纯文本报、摘掉 node 报「跑不了就是没验」。再往后四组：真仓库入口绕过 0 命中、走 `run_check` 不误报、影子文件 `rogue.py` 被抓 2 条；看板词改成 `["进行中"]` 红 19→15、清空抛错；字段面撤掉两条声明各报一条、真库 `distinct` 与折法键集合相等。代价与未决见 `docs/20261126_16_不适用声明要能证伪.md`（`DOC-58`）与 `docs/20261126_17_判据的边界.md`（`DOC-59`）。

### 工位契约、缺口认领、整改单（I / J）

前三层都在答「这个值我认不认识」；这一层答「**这个行交不出工位，谁负责补**」。工单写「1号工位」、主数据写「产线A-01」，60 张单对不上 60 张。最省事的接法是拿 `md_device.work_station` 反推，实测只对 **20/60**、**40/60 与设备台账矛盾**（WO-0002 写 2 号工位，它那台 CX-102 却在产线A-01）——所以**后端不做推导**：`md_station` 给稳定键 `station_id`（`S-A01`…`S-D04`），明细侧按契约提交 `station_id` 或（`prod_line` + 工位文本），后端只查字典。文本不是工位写法（如「4号线」）、产线与业务码说反、(line,seq) 查不到或查到多个——全判红，不猜、不折、不给默认值。

判据 **I**（`scripts/check_station_contract.py`）报违约行与单号，并先查契约自己：`md_station` 有没有 `station_id` 列、id 合不合 `S-[A-D]\d\d`、有没有重复、是不是登记过的 16 行。判据 **J** 读 `data/mapping/data_contract.json` 的 `field_gaps`：没主没期或过期仍违约 → 红；已认领未过期 → 黄（不判死，但必须出现在输出里）；违约归零 → 自动闭合。到期自动转红——「暂时」不会变永久，是因为判据不认这个词。

判据红了要有一张能追的纸：`gov_remediation_order`（`mock-mes/sql/012_remediation.sql`）+ `scripts/raise_remediation.py`。同一契约同一张表只允许一张 open，重复跑是更新不是新建；`--close` **复跑归零才允许关闭**，剩 60 / 302 行时直接拒绝，关闭时把「复跑时间 + 违规 0」写成证据。`scripts/verify_station_governance.py` 七段自证：补 1 行 → 59 且解析 `S-A01`；probe 三行小表走完开单→拒绝→补全→关闭；到期反例；真库回原样。

```powershell
# 工位契约（I）与缺口认领（J）：60 / 302 行违约 + 两条缺口黄；红就 exit 1
venv\Scripts\python.exe -X utf8 scripts\check_station_contract.py
venv\Scripts\python.exe -X utf8 scripts\check_station_contract.py --today=2026-12-10   # 到期反例：黄转红

# 整改单：--raise 开/更新，--close 只认归零，--list 看单子
venv\Scripts\python.exe -X utf8 scripts\raise_remediation.py --list
venv\Scripts\python.exe -X utf8 scripts\verify_station_governance.py                 # 七段 ALL OK
```

证据与拒绝掉的那条路在 `docs/20261126_20_不兜底.md`（`DOC-62`）。

### 缺陷字段面（K）

缺陷页从 T10 起就把 5 行脏数据染了色：`rec 4` 未来时间（`2027-01-01`）、`5`/`6` 置信度 150 与 -10、`8` 批次空串、`10` 批次 NULL。但 Python 侧一条判据都没有——**颜色是页面自己判的，治理结论里什么都没发生**。这是 `DOC-60` 的同一种病换了个页面。

判据 **K** 补三条，登记在 `data/mapping/field_rules.json`，实现是 `app/ds/field_checks.py`：

- `conf_pct_range`：0–100，区间闭合；空值算「没给」不算「给错」；
- `captured_at_not_future`：**判定时刻可注入**（`--now`），否则页面取浏览器钟、判据取库钟，边界行会随机假红；
- `lot_no_blank_split`：**NULL 与空串分开**——查映射 vs 查录入，合并成一个「空」就把契约 R2 又抹平一次。

**另起一族、不塞进 `checks.py`**：`run_check` 的 ctx 是客户词表（按客户不同），缺陷规则没有客户差异；硬塞会让 E 判据拿工单行去喂它们。但约束不放松——同一入口 `field_checks.run()`、登记没实现就抛 `UnknownCheck`、F（入口绕过）的 AST 扫描扩到这一族。

```powershell
# 判据 K：红 5 行（4 / 5 / 6 / 8 / 10），exit 1，末尾落台账 run_id
venv\Scripts\python.exe -X utf8 scripts\check_defect_fields.py

# 验收：登记↔实现、阈值同源、按 rec_id 对账、改错阈值必须红、台账多一行
venv\Scripts\python.exe -X utf8 scripts\verify_defect_fields.py
```

实测评据 K 的红行 = 页面染色的行，按编号逐个相同（`['4','5','6','8','10']`）；反例把 conf 上界改成 200，对账报「conf: 页面多 ['5']，判据多 []」——对账真的会红。顺带拆掉第三份阈值：`mock-mes/scripts/verify_ui.py::expect_defect` 的 `100/0` 改为从登记读。缺陷页也补了一行 KPI：`脏行 5 / 302（越界 2 · 未来 1 · 空批次 2）`。证据在 `docs/20261126_22_缺陷字段面.md`（`DOC-64`）。

### 判据自己有没有在工作（可观测性）

前四层都在判数据，没有一层判**判据**。判据要靠人开终端，今天跑了不等于明天跑；结果停在 stdout，362 行有没有变化要靠记忆；库里的数据新不新，没有任何一页能答。这一层补的是证据，不是再加一条判据。

`mock-mes/sql/013_observability.sql` 建 `gov_check_run`（`check_id / ran_at / duration_ms / status ∈ ok|warn|fail|error / red_count / dataset / code_hash / details`），`app/ds/runlog.py` 提供 `record` / `latest` / `staleness_hours`。两条判据脚本改成**默认落台账** + `--no-record`：`check_station_contract.py` 的 `red_count` 是**违约行合计**（`details.red_metric=violating_rows`），`check_status_policy.py` 的是**失败判据条数**（`failed_checks`）——两种量不混成一个数，否则趋势线没有意义。两条硬规则：写台账失败要打 `stderr`（观测缺一条不能让结论变绿）；`staleness_hours()` 从未跑过返回 `None`，调用方必须当红。

数据侧三个信号在 `mock-mes/api/obs.py`：接口指标（300 样本环形缓冲，`p50/p95/max`、`2xx/3xx/4xx/5xx`、最慢 5 条、`top_paths`）、数据新鲜度（9 张表白名单，`stale>24h`，**`future` 单列**——`-849 h` 不是「很新」而是未来时间脏值）、运行台账（每条判据 `hours_since` / `silent>24h`）。接口是 `/healthz`（真连库，失败 503）、`/v1/obs/summary`（三块一次取全）、`/v1/obs/{api,freshness,runs}`；页面第五页「判据运行」。

```powershell
# 判据默认落台账，末尾打印 run_id；加 --no-record 只判不记账
venv\Scripts\python.exe -X utf8 scripts\check_station_contract.py     # I fail 362 行 / J warn
venv\Scripts\python.exe -X utf8 scripts\check_status_policy.py        # A–H 全 ok

# 在后端目录：六段验收（连库 / 计数 / p95 / 陈旧 / 未来时间 / 静默 / 不写台账）
cd ..\mock-mes; python -X utf8 scripts\verify_obs.py
```

实测：台账 `run_id=1 station_contract fail 362 95ms`、`run_id=2 status_policy ok 0 1546ms`；`verify_obs.py` O1–O6 全 `[ok]`（连库 82 ms、p95 84.7 ms、工单表 7497.7 h 陈旧、缺陷表 -849.3 h 未来、30 h 静默、`--no-record` 2→2）；`verify_ui.py` 10 组 ALL PASS。

顺手查出验收脚本自己的一个 bug：`verify_ui.py` 的 `text()` 用非贪婪正则取 `id="kpi"` 的内容，遇到内层 `<b>…</b>` 就在第一个 `</b>` 前停住——之前所有 KPI 断言比的都是被截断的字符串（只判空，所以没造成假绿）。改成取整个 `<span id="kpi">` 段再匹配。证据在 `docs/20261126_21_可观测性.md`（`DOC-63`），未做的四条风险（4xx 未拆路径、24 h 阈值是拍的、指标只在内存、无告警通道）也写在那里。

### 中控台与推送（T23）

可观测性回答「判据跑没跑」，回答不了客户的下一句：「那谁来做、做到什么算完？」判据红了只是屏幕上的十几行字，整改单要人手工开——**系统很诚实，但没人在听**。

T23 把「红」变成一条**事件**：表 `gov_alert`（`mock-mes/sql/014_alerts.sql`）用 `dedup_key` 做身份、`seen_count` 数「存在几轮」、三态 `open/acked/closed`，唯一索引 `where state <> 'closed'` 让「复发」也可数。判定规则在 `app/ds/alerts.py`（纯函数，不连库）：

- **同一批红再跑一次不推**——只有「从绿转红」或「红行出现新编号」才叫醒人，其余只涨 `seen_count`；
- **变绿只写「复跑归零」，不写「已修复」**——修没修好由复跑证明；
- **判据静默单独一类**——它红的是「没人看」，不是数据；
- 行级按**编号比集合**，老台账只有条数时标 `baseline`，不假装「新增 0」。

页面是同一进程里的第二张皮（深色，`/console` 与 `/v1/alerts` 都在 8021，不另开服务），**只读、不给关闭按钮**：按钮会把「关掉」与「修好」变成同一个动作。推送顺序是 `判据 → 台账 → 事件 → 通道`：默认只落投递箱，`--webhook` 才外发，**外发失败写进 `detail.delivery` 且不当作已推**。

```powershell
# 扫最近两次运行 → 开/关事件（有新增 exit 1，没事 exit 0）
venv\Scripts\python.exe -X utf8 scripts\raise_alerts.py
venv\Scripts\python.exe -X utf8 scripts\raise_alerts.py --list
venv\Scripts\python.exe -X utf8 scripts\raise_alerts.py --ack=1 --by=张工
venv\Scripts\python.exe -X utf8 scripts\raise_alerts.py --close=1 --reason="复跑归零，2026-12-01 验收"

# 验收：事件状态机 A1–A7
venv\Scripts\python.exe -X utf8 scripts\verify_alerts.py
```

实测：2 条活事件（工位归属 362 行 / 缺陷字段 5 行），轮次看页面当前值（只会涨，不写死）；页面环形 362 / 5 / 0（98.6% 集中在工位归属）。`verify_alerts.py` A1–A7 全 `[ok]`，`mock-mes/scripts/verify_console.py` C1–C9 ALL PASS（无外网资源、五个数字与接口逐项相等、趋势恰三条），单测 18 条、全量 594。证据与「没做什么」（无值班表、阈值是拍的、无实时推送）在 `docs/20261126_23_中控台与推送.md`（`DOC-65`）。

### 一条命令巡检 + 扫描自检（T24）

上一版有个洞：**事件是扫描生成的，但扫描本身没记录**。页面显示「未处理 2 · 违约行 367」，却答不出这组数字是五分钟前还是上周——判据静默有 KPI，扫描静默没有。

四件事：

- 每次真扫完落一行 `check_id=alert_sweep`，`red_count` 口径是**新增推送条数**（`details.red_metric=pushed_events`），不是违约行数；
- 中控台第三张卡改成「事件扫描」，值取扫描年龄；超过 24 h 或从未扫 → 卡片转红 + 顶部横幅写明恢复命令；
- `scripts/patrol.py` 一条命令按顺序跑完：三道判据 → 事件扫描。退出码 0 无事 / 1 有新事件 / 2 有环节崩了（**崩了不许当红也不许当绿**）；
- 顺手补两处：`/v1/obs/api` 增 `errors_by_path`（以前只有 `errors: 31`，答不出哪条接口在坏）；`obs.record` 跳过 `/favicon.ico`（浏览器自己来要的 404 不是接口故障）。

```powershell
# 一条命令巡检（默认只落投递箱；有新增事件 exit 1）
venv\Scripts\python.exe -X utf8 scripts\patrol.py

# 验收：P1–P6（一条命令 / 自检口径 / 不重复推 / 错误按路径 / 横幅 / 静默置位）
venv\Scripts\python.exe -X utf8 scripts\verify_patrol.py
```

实测 `patrol.py --quiet` 输出 `[红] station_contract` / `[绿] status_policy` / `[红] defect_fields` + 扫描 `run_id`；自检行 `alert_sweep ok pushed 0`，复跑 exit 0 且自检行 +1。静默横幅用「把三行 `ran_at` 临时改到 30 h 前、截完逐行还原」验过（`out/ui/console-stale-1280.png`）。`verify_patrol.py` P1–P6 全 `[ok]`，`verify_alerts` / `verify_console` 仍全 PASS，单测 594。证据在 `docs/20261126_24_巡检与自检.md`（`DOC-66`）。

### 收口演示：认领 + 补一行 + 复原（T25）

前两轮证明了「判据会红」「数字是现在」，没证明**有人动手之后系统跟得上**。`scripts/demo_closeout.py` 一条命令把三段连起来，默认全程可回放：

- 认领：`#1 open → acked`（`--by` 必填，没有责任人的认领等于没认领），`seen_count` 不动；
- 补一行：拿 `md_station` 的「产线A」试算 `(产线A, 第1工位) → S-A01`，真写一行 `prod_line` 再复跑 → **362 → 361**，且只少 `wo_station_identity#WO-2026-0001` 这一条编号；
- 复原：`prod_line` 回 NULL + 复跑 → 362，**整表指纹与基线逐字相同**（`df7c564b4185077b`）；复原放在 `finally`，崩了也先退回去再报错。

顺手补的洞：趋势线纵轴从 0 起，1 行变化只有 0.1px，图上永远平——不改纵轴（拉开等于把噪音画成悬崖），改为数字右侧加「较上一次」`−1 / ±0 / +n`，`verify_console.py` 加 C7c/C7d 锁住。

```powershell
# 六段全套（结束复原；加 --keep-ack 认领留在页面上）
venv\Scripts\python.exe -X utf8 scripts\demo_closeout.py
venv\Scripts\python.exe -X utf8 scripts\demo_closeout.py --dry-run          # 只打印计划
venv\Scripts\python.exe -X utf8 scripts\demo_closeout.py --shot=out\ui\console-fix-visible-1280.png

# 验收：V1–V6（不写库 / 只少一行 / 复原三项一致 / 认领还原 / 不留台账 / 独立重算）
venv\Scripts\python.exe -X utf8 scripts\verify_demo_closeout.py
```

实测：`362 → 361 → 362`，`missing=['wo_station_identity#WO-2026-0001']`，指纹两侧同为 `df7c564b4185077b`；页面 367 → **366**（工单 361，趋势 `−1`，事件 #1「已认领」）；`verify_demo_closeout.py` V1–V6 ALL PASS；反例 `--line=产线Z` → `exit 2` 且不写库。代价写清：每次彩排留两行台账（361/362）、`seen_count` +1。证据与「那一行是假设不是真相」的口径在 `docs/20261127_25_收口演示.md`（`DOC-67`）。

### 常见问题目录：让客户数得出「有多少种毛病」（T26）

上一版中控台只有两条事件，看着像「只做了两件事」。原因不是判据少——契约断言、隔离区、源端值域三处早就在红/黄，只是**没落台账的判据在中控台上等于不存在**。这一轮补两件事：

- **登记客户侧 8 类问题**（归属 / 字段 / 隔离区 / 值域 / 结构 / 口径 / 逾期 / 静默）：`app/ds/issues.py` 是唯一源，`scripts/seed_issues.py` 同步进 `gov_issue_catalog`，页面报「登记 8 类 · 今日命中 4 类」，两个数分开念；
- **把已有判据接进事件流**：`scripts/collect_issues.py` 子进程跑 `check_data_contract.py` / `check_source_values.py` 的 `--json`，只搬运作台账，不做第二套判定。

三处口径写死（错一处客户就会问）：`red_count` 三种单位（行 / 件 / 列，标题按单位换字）、值域判 `warn` → 事件必须是黄、静默的样本不许取 `alert_sweep.red_count`（那是推送条数）。

```powershell
# 目录（改 Python 后同步；--check 只比对）
venv\Scripts\python.exe -X utf8 scripts\seed_issues.py
venv\Scripts\python.exe -X utf8 scripts\seed_issues.py --check

# 接线（--no-record 只看不写；exit 1 = 有红或有黄）
venv\Scripts\python.exe -X utf8 scripts\collect_issues.py

# 验收：I1–I5（目录一致 / dry 不写库 / 只多两行且契约数=隔离区行数 / 事件带类别 / 0 样本不建事件）
venv\Scripts\python.exe -X utf8 scripts\verify_issues.py
```

实测：`data_contract fail 1 件`（隔离区 `ts_in_future` 1 条）+ `source_values warn 1 列`（`conf_pct` 150 / −10），台账 `run_id=46/47`；事件 4 条（红 3 / 黄 1），页面 `4 / 8 类`、目录 8 行、事件流带「类别」列、趋势 5 条；`verify_issues.py` I1–I5 ALL PASS，`verify_console` C1–C10e ALL PASS。截图 `mock-mes/out/ui/console-catalog-1280.png` / `-1024.png`；口径与「剩下 4 类怎么说」在 `docs/20261127_26_常见问题目录.md`（`DOC-68`）。



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
              check_status_policy（A–H）/ check_station_contract（I/J）/ raise_remediation / verify_station_governance
              raise_alerts / patrol / demo_closeout / verify_alerts / verify_patrol / verify_demo_closeout
              seed_issues / collect_issues / verify_issues（常见问题目录与接线）
             check_ctx / prune_checkpoints / start.ps1 / audit_verify
docs/        工程清单、W0/W1/M3/M4、意图层改造、Checkpointer、流式输出、文档对账、
             校验层引用式溯源（08）、护栏架构设计参考（09，跨项目可搬）
             数据治理线：口径登记（DOC-52）、源端 S1–S4 漂移（DOC-53）、值域 S5（DOC-54）、换源验收（DOC-55）、
             量纲判据改中位数（DOC-56）、换客户折法验收（DOC-57）、不适用声明要能证伪（DOC-58）、
             判据的边界：入口/词表归属/字段面（DOC-59）、同一条规则写了四遍（DOC-60）、
             数据治理线 · 一页（DOC-61，现场材料）、`不兜底`：工位契约 / 缺口 / 整改单（DOC-62）、
             中控台与推送（DOC-65）、巡检与自检（DOC-66）、收口演示：认领/补一行/复原（DOC-67）、
             常见问题目录与接线（DOC-68）
             00_文档索引.md 是全部 69 篇（`DOC-01`–`DOC-68` + `DATA-01`）的状态表：哪篇能引用、哪篇只是历史，先看它
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
12. **收口演示会留痕**：`demo_closeout.py` 每次彩排写两行台账（361 / 362），事件 `seen_count` +1。
    数字会动是它的目的，但「362」这个基线每彩排一次都要重新解释一遍；不想留痕就 `--no-record`，
    代价是页面不跟着动（`DOC-67` 第五节）
13. **常见问题目录是「登记 8 类、今天命中 4 类」，不是 8 类全覆盖**：结构漂移今天没有样本（页面写「未跑」）、
    口径判据全绿、没有逾期整改单、判据都在跑；`check_db_floor`（库约束 vs 设计，2 条不符）跑在 sandbox 且会插脏数据，
    属于平台自检，**故意没接进客户库的事件流**；数据新鲜度（陈旧 4 表 / 未来 1 表）仍只在 KPI，
    因为 `fde-diagnosis-agent` 没有与 `mock-mes/api/obs.py` 同一份「表-时间列」映射，搬过来就是第二套口径（`DOC-68` 第七节）
