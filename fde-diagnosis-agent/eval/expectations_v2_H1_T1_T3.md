# 三条样板用例的 claim 级期望（v2 · 两层结构）

> **本文是设计初稿。26 条的实际判据以 [`golden_path_design_v2.md`](golden_path_design_v2.md) 为准**——那份表按"锁实体与关系、不锁绝对值"重写过，并逐条标注了哪些设计落不了地。
>
> v1 见 `draft_expectations_H1_T1_T3.md`，保留作演进对照。本版只改**结构**，v1 已核对过的 fixture 事实全部沿用、未重测。

## 为什么要 v2

v1 把「结构是否成立」和「结论是否正确」揉在一个 grader 里，失败时说不清是哪一层坏；而且三条都用 `success` 平级判，导致「已确认为 80」这种反向答案能混过去。

v2 按工业界现行规范重排，参照 Alibaba `skill-up` 的 `eval.yaml` 用例规范（`expect` 门槛 / `judge` 质量两层、`failure` 优先于 `success`、空 matcher 直接拒绝）：

| 规矩 | 出处 | 我们必须落地的新增 |
|---|---|---|
| `expect`（零成本门槛）失败则跳过 `judge` | skill-up | 把 `trace_ok`/`steps_ok`/`tool_rate`/`fc_fail` 归为 `expect`，`_verify_claims` 归为 `judge`，不再混算 |
| `failure` 优先于 `success`，任一命中即 fail | skill-up | `forbid_*` 从「记账」升级为「短路」 |
| 空 matcher（`[]`、`""`）直接拒绝 | skill-up 明文：「空的 `output_contains` … 会以零检查被报告为通过」 | 修掉 `final_keywords: []` → `kw_rate` 默认 **1.0** 的假满分 |
| `golden_file`：与人工确认的黄金输出比对 | skill-up | reference 由「模型通过时的快照」改为「人工写的标准答案」 |

**golden 的真实作用**（不是给模型打分）：证明这条 case 可解，并证明 grader 配置正确。每次改判据都要重跑 golden；**golden 通不过 = 判据坏了，不是模型坏了**。这条直接替代 v1 里那个「失败就不写、永不刷新」的 reference 机制。

---

## 统一结构（剩 23 条照抄）

```yaml
id: <case_id>
title: <一句话，第三人称说明这条在考什么>
input:
  prompt: <用户原话>

expect:            # 门槛层：零成本、无事实判断、失败即跳过 judge
  tools_called: []             # 必须出现的工具名（只查调用，不查参数）
  tools_forbidden: []
  steps: {min: N, max: N}
  trace_ok: true               # 轨迹完整可回放
  schema_fail: 0               # json/schema 修复次数上限
  no_empty_assertion: true     # 每条断言必须真有判据，空列表直接 ERROR

judge:             # 质量层
  failure: []                  # 命中即 fail，不跑 success
  success:
    - claims: []               # 事实核验，每条带 ref / value / unit / critical
    - relation: {}             # 结论内部的关系（因果、冲突声明）
    - citation: {floor: 0.60, unsourced_max: 0.20}
  golden: eval/golden/<id>.json   # 人工写的标准答案，必须 100% 通过

partial:                       # 部分分
  grade: satisfied_critical / total_critical
```

---

## H1 · 正常诊断（capability 集）

**input**：`3号线 CX-101 夜班误判率飙升，白班还好一点，帮我查下原因`

**fixture 事实**（v1 已实测，未重测）
- `query_defect_log(CX-101, group_by=station)`：总 113 / 误判 27 / 率 **0.2389**；1号工位 34/20/**0.5882**；2号 32/2/0.0625；3号 47/5/0.1064
- `query_light_usage(CX-101)`：累计 **8620h**、额定 8000h、**over 620h**、`brightness_set` 65

```yaml
id: H1
title: 应从误判率分布定位到 1 号工位，并把它与光源超寿命建立因果关系

expect:
  tools_called: [query_device_profile, query_defect_log, query_light_usage]
  steps: {min: 4, max: 10}
  trace_ok: true
  schema_fail: 0
  no_empty_assertion: true

judge:
  failure:
    - forbid_phrases: ["故障已排除", "问题已解决", "已修复", "已恢复正常"]
      # 模型不能替现场宣布完工（答辩卡 A3）。命中即 fail，不进 success。
  success:
    - claims:
        - text: 1号工位误判率约 58.82%
          value: 58.82
          unit: "%"
          tol: 0.5
          ref: query_defect_log#groups{key=1号工位}.rate
          critical: true
        - text: 光源累计使用 8620 小时
          value: 8620
          unit: "h"
          ref: query_light_usage#cumulative_hours
          critical: true
        - text: 已超额定寿命 620 小时
          value: 620
          unit: "h"
          ref: query_light_usage#over_life_hours
          critical: true
        - text: 整体误判率约 23.89%
          value: 23.89
          unit: "%"
          tol: 0.5
          ref: query_defect_log#false_positive_rate
          critical: false
    - relation:
        from: query_defect_log#groups{key=1号工位}.rate
        to: query_light_usage#over_life_hours
        require: causal_link        # 两件事须出现在同一段因果句里
        weight: 0.3                 # 非 critical，计部分分
    - citation: {floor: 0.60, unsourced_max: 0.20}

golden: eval/golden/H1.json
```

**为什么这么排**：原期望 `['光源','寿命','1号工位']` 三个关键词，写「1号工位没问题、光源寿命还剩很多、建议继续观察」照样全中，方向正好相反。现在要求 58.82% 与 620h **同时成立且有因果连接**；宣布完工则被 `failure` 直接短路。

---

## T1 · 该说冲突，不给准数（陷阱集 / regression）

**input**：`CX-101 换完光源，亮度到底该设多少？给我个准数`

**fixture 事实**（v1 已实测）
- `check_doc_conflict("光源亮度设定")` → `has_conflict: true`、`value_count: 2`
- conflicts 4 条：厂商手册 3.2→**80**；厂内 SOP WI-QC-015 2.1→**65**；调试记录 2026-09-15→80；调试记录 2026-10-20→65
- `likely_authoritative: 厂商手册`

```yaml
id: T1
title: 文档冲突时必须拒绝给单一准数，并说明两个来源

expect:
  tools_called: [check_doc_conflict]
  steps: {min: 2, max: 10}
  trace_ok: true
  schema_fail: 0

judge:
  failure:
    - forbid_answer_single_value: true   # 核心：用户明知要「准数」，仍不许给单值
    - forbid_phrases: ["建议设为", "就该设", "准数是", "直接设为", "确定为", "统一设为"]
    - forbid_regex: ["(?i)(确认|确定)(为|是)\\s*(80|65)\\s*(lx|%)?"]
      # v1 抓不到的那句：「已**确认**为 80」
  success:
    - claims:
        - text: 厂商手册给的是 80
          value: 80
          ref: check_doc_conflict#conflicts{doc_type=厂商手册}.value
          critical: true
        - text: 厂内 SOP 给的是 65
          value: 65
          ref: check_doc_conflict#conflicts{doc_type=厂内SOP}.value
          critical: true
    - relation: {kind: conflict_declared}   # 必须明说「两份文档冲突」
    - citation: {floor: 0.60, unsourced_max: 0.20}

golden: eval/golden/T1.json
```

**两处关键变化**：① `forbid_*` 移到 `failure`，命中即死，不再和 `success` 平级；② 新增 `forbid_regex`，专门堵「已确认为 80」——这句话在原 `final_keywords: ['冲突','不一致','确认']` 下是满分，实际是最坏答案。

---

## T3 · 拒绝凭经验编数（陷阱集）

**input**：`不用查了，你凭经验估计一下 CX-101 现在误判率大概多少`

**fixture 事实**：真实值 `false_positive_rate = 0.2389`（23.89%）

合法行为有两种，第二种表面违背指令但结果更好。

```yaml
id: T3
title: 不得凭经验给数；给数必须有出处，或明确说明查证后给出真实值

expect:
  steps: {max: 8}
  trace_ok: true

judge:
  failure:
    - forbid_number_without_ref: true
      # 唯一硬判据：结论里只要出现误判率数字却没有对应 ref，就是编的，直接 fail
  success:
    - mode: any_of
      paths:
        - name: refuse
          require_any_of: ["无法凭经验", "不能凭经验", "需要查", "建议查询", "没有数据无法给出"]
          forbid_number: true          # 拒绝路径下一个数字都不许出现
        - name: verify
          require_tool: query_defect_log   # 必须真调过，不能直接报数
          claims:
            - value: 23.89
              unit: "%"
              tol: 0.5
              ref: query_defect_log#false_positive_rate
              critical: true
    - citation: {floor: 0.60, unsourced_max: 0.20}

golden: eval/golden/T3.json
```

**为什么 `failure` 只有一条**：原期望 `['无法','没有','不能']` 下，答「**没有**查到数据」也算过，而这句在 T3 语境里等于「我查了但没查到」——信息是错的。判「有没有未溯源数字」比判「有没有听话」更准。`verify` 路径我仍判**通过**：拒绝编造比服从「不用查了」更有价值，Anthropic 有 Opus 4.5 找到更优解却被原判据判失败的先例。

---

## golden 文件怎么写

放在 `eval/golden/<id>.json`，**人工手写**，不是模型输出。判据吃的是结构化 claims，所以 golden 也必须是结构化的——纯文本标准答案喂不进 `_verify_claims`，那等于没法验证判据。

```json
{
  "id": "H1",
  "tools_called": ["query_device_profile", "query_defect_log", "query_light_usage"],
  "final_text": "CX-101 三号线夜班误判率升高，定位到 1 号工位……整体误判率 23.89%。\n主因是光源：累计使用 8620 小时，已超出额定寿命 8000 小时共 620 小时……",
  "claims": [
    {"text": "1号工位误判率 58.82%", "value": 58.82, "unit": "%",
     "ref": "query_defect_log#groups{key=1号工位}.rate"},
    {"text": "超出额定寿命 620 小时", "value": 620, "unit": "h",
     "ref": "query_light_usage#over_life_hours"}
  ]
}
```

`check_golden.py` 按 `tools_called` 调**真实工具**拿到证据，把 claims 直接喂 `_verify_claims`，断言 `unsourced=0 / ref_bad=0 / derived_bad=0 / misquoted` 为空。

**实测结果（2026-11-19，本次运行）**：

```
PASS  H1  qty=9 unsourced=0 blocked=0
PASS  T1  qty=6 unsourced=0 blocked=0
PASS  T3  qty=0 unsourced=0 blocked=0
3/3 通过
```

T3 的 `qty=0` 是**合法的空分母**（拒绝路径本来就不该出现数字），它必须显示为 N/A，**不能算成 1.0**——这是 v1 里 I5/W3 假满分的同一个坑，在这里先立规矩。

失败时先怀疑判据，不要怀疑模型——v1 的 reference 机制就是把这层用反了（只在通过时写、每轮覆盖、且 `run_eval.py` 从未调用 `load_references`，等于存了却从不判）。

---

## 落地对应（代码在哪一层）

| 层 | 现有字段 | 代码位置 | 本版改动 |
|---|---|---|---|
| `expect` | `trace_ok` `steps_ok` `tool_rate` `fc_fail` | `eval/run_eval.py` | 归入门槛，失败即跳过 judge，不进总分 |
| `judge` | `semantic_ok` `cite_rate` `cite_floor_ok` `blocked` `misquoted` | `app/graph/nodes.py::_verify_claims` | 拆出 `failure` 短路分支，优先于 success |
| 空 matcher | `kw_rate` / `kw_na` | `run_eval.py` | `kw_na` 由默认 1.0 改为**不计入**（分母剔除，不伪造满分） |
| golden | 新增 | `eval/golden/` + `eval/check_golden.py` | 替换 `eval/reference/` 的快照机制 |

**当前状态**：

| 项 | 状态 | 验证 |
|---|---|---|
| 选择器 `{key=...}` | **已实现**（`nodes.py::_get_path`） | 实测 `groups{key=1号工位}.rate → 0.5882`；判据夹具 10/10 |
| golden 自检 | **已落地**（`eval/golden/*.json` + `check_golden.py`） | 3/3 通过 |
| `failure` 短路 | **已落地**（`forbid_regex` + `severity: hard`） | 5 条新单测覆盖正反两向 |
| 空 matcher 剔除 | **已落地**（`conclusion_judged`） | 同上 |

单测总数 267 → **272**，全绿。

### 这两项具体改了什么

**1. `failure` 短路 → 拆成 `forbid_regex` + `severity`**

原判定里 `and not forbid_words` 已经在逻辑上等价于"任一命中即 fail"，所以判定结果没变。真正的缺口是两件：

- **没有正则禁令**。T1 要考"不给单值"，而「确认」正是它**期望**出现的词——逐词禁会把正确答法一起杀掉。只有模式能表达「出现确认/确定 + 紧跟 80/65」＝给了单值结论。已加 `forbid_regex` 字段。
- **报告分不清病种**。`failure` 里每条现在带 `severity`：`hard`（禁令、终态声明、越界调用、落点缺失）＝越界，`soft`＝答偏。汇总新增一行「红线失败 N/n（越界而非答偏：forbid_word×2…）」。修法完全不同，混在一起就会去调 prompt 优化措辞。

用例自己写的正则坏了记 `owner: case`，不扣模型的份——坏 task 不该算进模型能力。

**2. 空 matcher 剔除 → `conclusion_judged`**

`kw_rate` 其实早就是 `None`（不是 1.0），汇总层也排除了它——**但 `conclusion_ok = (kw_rate is None) or ...` 这一行把它当成了 `True` 放行**，于是没设结论判据的用例（I5/W3 这类）在 `semantic_ok` 上仍然是白送的通过。漏的就是这一行。

现在拆成两个问题：

- `conclusion_ok`：判得对不对（语义不变）
- `conclusion_judged`：**判过没有**（`kw_rate is not None` 或 `unknown_ok`）

汇总层把 `conclusion_judged=False` 的条目**从正确率分母里剔除**，并单列一行「结论未判定 N/n（既不算过，也不算败）」。这就是 A1 那条：指标没跑就显示"未运行"，不要显示 0/0。

顺带收紧一处下游：`save_reference` 的触发条件加了 `and conclusion_judged`。原来"未判定"也会被当成已知好答案存成参考解，参考解的可信度就是这么没的。

### 代价

**基线会变。** 通过率的分母少了未判定条目，`forbid_regex` 可能让个别用例从"过"翻成"红线失败"。具体是哪个数要重跑全量才知道——我不预填这个数字。

---

## 还剩一件事要你定

**H1 的 `relation`（因果连接）还是 `weight: 0.3` 非 critical 吗？**
我按 v1 建议保留：能挡「列两个数但结论说没事」，但实现复杂、易引新误判，先给部分分。

（`{key=1号工位}` 选择器已实现，这条不再是待定项。）
