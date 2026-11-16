# Checkpointer 持久化与上下文管理

日期：2026-11-16
范围：`app/graph/persistence.py`、`state.py`、`nodes.py`、`build.py`、`app/web/server.py`、`static/index.html`
新增脚本：`scripts/smoke_multi_turn.py`、`scripts/check_ctx.py`、`scripts/prune_checkpoints.py`、`scripts/smoke_web_session.py`

---

## 一、要解决的不是"加个数据库"，是追问即死

改造前实测到的三条完整失败路径（同一台机器、同一份代码）：

| 上一轮系统说什么 | 用户照做 | 系统回什么 |
|---|---|---|
| "CX-101 你想先看哪一方面？……**回一句「看数据」**" | `看数据` | "需要两个信息才能开始：1. 设备编号……" |
| 同上 | `CX-101` | "CX-101 你想先看哪一方面？……" |

系统在一条追问里给了两条互斥指令，两条都走不通。根因不在措辞，在图结构：

```python
g.add_edge("ask_user", END)   # 追问即结束，state 随进程/请求一起丢弃
```

`ask_user` 之后 `END`，`state` 没有任何落地的地方。下一次请求是全新的空 state，所以"用户刚才答了什么"根本不存在。

产线上的人说话本就是零碎的（"那台老出问题的怎么回事"），一次问不齐是常态。**这不是边角场景，是主路径**。

---

## 二、方案：LangGraph Checkpointer + PostgreSQL

### 2.1 为什么是它

| 方案 | 能解决 | 代价 |
|---|---|---|
| A. Checkpointer（选定） | 追问接上；顺带拿到断点恢复、跨进程续跑 | 引入一张会持续增长的表，要配清理 |
| B. 自建 session 表 | 完全可控 | 两套状态（自己的表 + 图内的 messages）容易不同步，改一处漏一处 |
| C. 前端点按钮补全 | 固定路径 | 只解决设计时想到的那几条路，用户否决过选择框式提问 |

选 A 的额外收益是它和项目一的 durable execution 是同一套能力：`thread_id` 就是会话主键，进程重启后拿同一个 id 还能接着跑——这一点下面有实测，不是纸面结论。

### 2.2 落地形状

```python
# persistence.py
def get_saver():
    """进程内单例。长驻服务每轮新建连接会把 PG 打满，所以手动 __enter__ 后一直持有。"""
def new_thread_id() -> str:   # "tmp-<uuid4 前12位>"

# build.py
build_graph(use_checkpoint=True)  → g.compile(checkpointer=get_saver())
run(user_msg, thread_id=None)     → app.invoke({"user_msg": msg}, {"configurable": {"thread_id": tid}})
```

三点值得讲：

1. **`init` 只传 `user_msg`**。首轮 state 本来就是空的，`messages` 由 `intake` 补；之后每轮只给新的这句话，其余字段（`slots`、`session_summary`、`messages`）从 checkpoint 恢复。如果像改造前那样把 `messages: []` 一起传进去，等于每轮把历史清空。
2. **`thread_id=None` 时自动生成**。评测和单次 CLI 不需要会话，各跑各的互不干扰；Web 端传前端存的 `session_id`。
3. **表和业务表同库**（`fde_rag`），`PostgresSaver.setup()` 幂等建 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`。setup 只跑一次，重复调用无副作用。

### 2.3 state 字段的两种生命周期

这是改造里最容易写错的地方：**不是所有字段都该累积**。

| 字段 | reducer | 为什么 |
|---|---|---|
| `messages` | `operator.add` | 多轮对话必须留 |
| `evidence` | `operator.add` | 溯源要能回查早几轮的工具返回 |
| `session_summary` | 字符串拼接 | 每轮追加一行 |
| `slots` | dict 合并 | 本轮确认的覆盖，没确认的保留 |
| `steps` | **每轮重写** | 上一轮花掉的 8 步不该算到本轮预算里 |
| `final` | **每轮重写** | 不清空的话新一轮会直接带着旧结论进 `verify` |
| `blocked` | **每轮重写** | 上一轮的数字告警与本轮无关 |
| `tool_names` | **每轮重写**（`intake` 清零，`act` 内合并） | 展示"本轮用了哪些工具"；一轮内可能多批次，所以 `act` 里合并而不是覆盖 |

`steps` 那个如果漏了，表现是第三轮开始 `MAX_STEPS` 直接超预算，模型一步都不敢走。

---

## 三、上下文管理：三层，各管一件事

多轮的真正难点不是"存下来"，是**存下来之后每轮往模型里塞多少**。

### 3.1 实测的膨胀速度

单轮 diagnose 内部，三次 plan 的 `prompt_tokens`（同一次运行，工具返回逐次累积）：

```
2085 → 2986 → 4067
```

同一轮内涨 2000 token，因为每次工具返回都进 messages。

第二轮 diagnose 的起点直接是 **5080**（带上了第一轮全部历史）。照这个斜率，第 10 轮起点会到 3 万 token 量级——DeepSeek 能吃下，但每次调用都在为已经答完的问题付费，延迟也跟着涨。

### 3.2 三层各负责什么

```
┌─ 轮内（Episodic）──── messages ────────────┐
│  本轮取证必须看到全部工具返回，一刀不裁          │
│  上限由 MAX_STEPS=12 间接控制（工具数≤12）      │
├─ 跨轮（Working）──── slots ────────────────┤
│  已确认：设备号、现象                        │
│  understand 靠它做槽位继承，不裁              │
├─ 跨轮（压缩）──── session_summary ─────────┤
│  被裁掉的历史，用每轮一句话摘要留在 system prompt │
└──────────────────────────────────────────┘
```

三个东西解决三个不同问题，混成一件事就会出问题：

- 只裁 messages 不留摘要 → 模型不知道前面查过什么，会重复查同一台设备
- 用摘要代替 slots → 设备号这类硬字段不该靠自然语言回忆，容易串台
- slots 不裁但 messages 全留 → 白留

### 3.3 裁剪规则：按"轮"切，不按"条"切

```python
MAX_CTX_TURNS = 2

def _context_messages(state):
    msgs = state.get("messages") or []
    starts = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    if len(starts) <= MAX_CTX_TURNS:
        return msgs
    cut = starts[-MAX_CTX_TURNS]
    head = msgs[:1] if cut > 0 else []      # 第一句原始诉求留着
    return head + msgs[cut:]
```

三条规则，每条都有原因：

1. **从 `user` 消息处切**。如果按条数从尾部切，会切断 `assistant.tool_calls` 和紧随其后的 `tool` 消息——OpenAI 协议要求每条 tool 消息都能对应到某个 tool_call_id，切散了直接报 400。这个坑只有真跑才会遇到，读代码看不出来。
2. **保留 `msgs[:1]`**。第一句是用户的原始诉求（"夜班误判飙升"），它是整场会话的锚点。只留最近两轮会让模型忘了这个人一开始关心的是什么。
3. **单位是轮不是 token**。按 token 裁需要先 tokenize，且切点仍会落在 tool 消息中间；按轮裁天然对齐协议边界。

### 3.4 被裁掉的信息怎么补回来

`finalize` 每轮追加一行摘要，拼进 `plan` 的 system prompt：

```
[2] 用户「看数据」→ lookup；工具 query_device_profile,query_defect_log；结论「### CX-101（3号线 / 1-3工位）…」
```

用规则拼而不调模型总结，理由：

- 可复现：同样的输入永远同样的摘要行，评测不用重新标
- 不花钱：一次额外 LLM 调用省掉了
- 出错可定位：字段是死的，一眼能看出哪轮丢了什么

代价是它不如模型总结"聪明"——它只有 80 字的结论开头，丢掉中间的推理过程。对"设备是哪台、查过什么"足够，对"上一轮为什么排除了批次"不够。这是有意的取舍，记在已知缺口里。

### 3.5 校验层的同步边界：verify 只认最近两轮证据

改 `verify` 时遇到一个必须一起改的地方：

```python
# 只用本轮和上一轮的工具返回做溯源
ev = [e for e in evidence if int(e.get("turn") or 1) >= cur_turn - 1]
```

为什么不继续拿全量 `evidence`：被裁掉的轮次，模型本轮根本没读到。如果还允许从那些证据里找数字，就等于"模型没看过的东西也能算溯源成功"——校验层的拦截率会虚高，而这个虚高正好出现在最该拦住的地方。

**裁剪和溯源必须用同一条边界**，否则两个模块各说各话。

---

## 四、实测结果

### 4.1 多轮能接上（`scripts/smoke_multi_turn.py`）

| 轮次 | 输入 | 意图 | 步数 | 结果 |
|---|---|---|---|---|
| 1 | `帮我看看最近怎么样` | clarify | 0 | 追问"设备编号 + 想做什么" |
| 2 | `CX-101` | clarify | 0 | 记住设备，改问"想先看哪一方面" |
| 3 | `看数据` | **lookup** | 2 | 直接出 CX-101 档案 + 误判统计表 |

改造前第 3 轮会重新问"设备编号"，现在能一路走到底。

换话题也正确：CX-101 诊断完再问 `那 CX-102 呢`，`slots` 切到 CX-102 且 **phenomenon 清空**——不把 A 机的"1号工位误判多"套到 B 机头上。

### 4.2 上下文裁剪真生效（`scripts/check_ctx.py`）

```
[1] 单测：24 条伪造 messages → 裁剪后 9 条
    首条："第一句原始诉求"（保留）
    保留的 user 提问：['第一句原始诉求', '第5轮提问', '第6轮提问']
    tool 消息与 tool_calls 配对完整：True

[2] 真实三轮：
    第1轮 messages 全量=8  送模型=8
    第2轮 messages 全量=9  送模型=9
    第3轮 messages 全量=10 送模型=3   ← 裁剪生效
```

### 4.3 checkpoint 真落库

该 thread 在 PG 里 **23 行**（3 轮 × 每轮约 7–8 个节点）。不是"编译进图了所以应该有"，是真查表数出来的。

### 4.4 跨进程恢复

第二次运行脚本（**全新 Python 进程**）复用上一进程留下的 `tmp-5a4b76639c8b`，输入 `看数据`：

```
恢复后 turn=2
slots={'device_id': 'CX-102', 'phenomenon': '批次误判多'}
```

上一轮的设备、现象、轮次全部从 PG 读回。这条对应面试里"断点恢复"那句，能现场演示。

### 4.5 Web 端多轮（`scripts/smoke_web_session.py`）

走真实 HTTP：同一 `session_id` 三轮 → turn 1/2/3，第 3 轮出数据表；换 session → turn 回到 1；`session_id="../../etc"` → HTTP 400。

### 4.6 单轮回归：先掉到 83.3%，修完回到 95.8%

改造后第一轮跑 24 条：**20/24 = 83.3%**，失败 H5 / W1 / W2 / W3。逐条查原因，发现是两件不同的事：

**W2 不是代码回归，是 mock QMS 挂了。** 模型在结论里写"QMS 系统连接被拒绝"——8010 端口没在监听。起服务后 W2 立刻恢复。这提醒了一件事：**回归失败要先看失败原因，不要先怀疑自己刚改的代码**。

**W1 / W3 是真回归，但根源是提示词。** 上一段加的规则第 7 条是"用户陈述与工具不一致必须指出"，本意是治"顺着用户错前提说"。它同时让模型变得过度保守：

- W1（用户给了根因 + 明确要求建单，但坚持"按 65 亮度"）→ 模型说"这个动作我不能照做"
- W3（用户说"反正有问题，先建单"）→ 模型说"工单不能建"

加第 8 条把两件事拆开：

> 指出不一致不等于拒绝执行。用户明确要求某个动作且根因已明确时就执行，有疑点的参数在结论里标出来让用户定，**不要替用户决定"这事我不做"。你是助手，不是审批人。**

修完 W1 4 步建单、W2 3 步幂等命中、W3 6 步建单并 `deduped=true`。

全量重跑：**23/24 = 95.8%**，唯一失败 H5（8 轮全败，故意保留）。

| 指标 | 改造前 8 轮均值 | 本次 |
|---|---|---|
| 语义正确率 | 90.0% | **95.8%** |
| 终局命中率 | 0.88 | 0.903 |
| 步数越界 | 2/192 | 0/24 |
| 意图判错 | 0/192 | 0/24 |
| fc 失败 | 0/192 | 0/24 |
| 耗时 | 5.5 s/条 | 5.9 s/条 |

耗时多 0.4 s/条，就是 checkpoint 写库的成本。**这个代价必须明说**：多轮能力的价钱是每条多 7%，换来的是追问能从"死"变成"活"。

---

## 五、checkpoint 表的增长与清理

| 指标 | 实测 |
|---|---|
| 59 个会话 | 648 个 checkpoint、1050 blobs、2446 writes、2.1 MB |
| 单会话 | 约 11 行、36 KB |
| 一天 100 次会话的估算 | 3.6 MB/天、约 1.3 GB/年 |

`scripts/prune_checkpoints.py` 两种模式：

```bash
python scripts\prune_checkpoints.py --dry-run        # 先看会删什么
python scripts\prune_checkpoints.py --days 7         # 删 7 天没动过的
python scripts\prune_checkpoints.py --keep 200       # 只留最近 200 个会话
```

实测执行：648 行 → **131 行**，59 个会话 → 10 个。

两个实现细节：

1. **`checkpoints` 表没有 `created_at` 列**。时间戳只在 `checkpoint->>'ts'` 这个 jsonb 字段里，所以清理 SQL 必须是 `max((checkpoint->>'ts')::timestamptz)` 聚合，不能简单按列过滤。
2. **三张表要按序删，不能只删 checkpoints**。`checkpoint_blobs` / `checkpoint_writes` 对 `checkpoints` 没有 `ON DELETE CASCADE`，先删主表会留孤儿行。

---

## 六、本轮踩的坑

| 坑 | 表现 | 修法 |
|---|---|---|
| `init` 传了 `messages: []` | 每轮历史被清空，等于没做持久化 | 只传 `user_msg` |
| `steps` 没每轮清零 | 第 3 轮起预算就超了 | `intake` 里重写为 0 |
| 裁剪按条数切 | 会切断 tool_calls/tool 配对，OpenAI 报 400 | 按 `user` 消息位置切 |
| `verify` 用全量 evidence | 被裁掉的证据也能"溯源"，拦截率虚高 | 按 turn 过滤，与裁剪同边界 |
| 提示词第 7 条太硬 | 用户明确要求建单也不做 | 补第 8 条区分"指出"与"拒绝" |
| `psycopg3` 的 row 取 `[0]` | `KeyError: 0`（PostgresSaver 设了 dict_row） | 按键名取 |
| `run()` 的 trace 分支漏赋值 | 返回的 `thread_id` 是空串 | 两个分支都设 |
| 回归失败当成代码问题查 | 白查 W2 | 先看失败原因，8010 根本没起 |

---

## 七、已知缺口

1. **摘要只有结论开头 80 字**。裁掉的推理过程（"为什么排除了批次"）没有保留，多轮深挖时会重复劳动。
2. **`MAX_CTX_TURNS=2` 是拍的**。有据可依的上界应该是"裁到 prompt 稳定在 X token"，现在没做 token 级控制。2 轮的实测依据是 5080 token 的起点，够用但没优化。
3. **轮内不裁**。单轮 12 次工具、每次返回 4000 字，最坏约 1.2 万 token，靠 `MAX_STEPS` 兜着，没有再压单条返回长度。
4. **没有按会话的并发保护**。同一个 `thread_id` 两个请求同时进来会互相覆盖 checkpoint（LangGraph 不拦）。Web 端一人一 session 天然隔离，多人共享 session 才会撞。
5. **prune 只有手动**。生产该挂定时任务，现在没做（本地演示够用）。
6. **H5 仍失败**。8 轮全败，方案"否证步骤"未实施。
