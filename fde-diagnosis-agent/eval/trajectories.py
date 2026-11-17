"""27 条 golden path 定义（25 条单轮 + M1/M2 两条多轮）。

I9 是补 query_work_order 时新增的：补工具不补用例，新能力就没有自动化保障。

多轮用例（`turns` 非空）自带一个单轮没有的判据 `round_ok`：最后一轮所有
`ref` 必须在**本轮还看得见的证据**里成立（`cite_oob` + `path_miss` + `ref_bad` == 0）。
没有这条判据，多轮用例跑的还是单轮那套指标，"进 eval"就白进了。

设计原则：每条只考一件事，期望写成可程序判定的形式（工具名、聚合维度、结论关键词）。
不写"应该诊断得对"这种没法判的话。

期望工具不是"必须全部调用"，而是"至少应该出现的证据来源"——
判定按命中率算，避免把合理的取证顺序差异判成失败（W0 踩过：arg_ok 用 first 判，导致取证顺序不同被判错）。
"""

TRAJECTORIES = [
    # ---------- H1-H5：硬件类 ----------
    {
        "id": "H1",
        "type": "硬件",
        "input": "3号线 CX-101 夜班误判率飙升，白班还好一点，帮我查下原因",
        "expect_tools": ["query_device_profile", "query_defect_log", "query_light_usage"],
        "forbid_tools": [],
        "expect_group_by": ["station"],
        "final_keywords": ["光源", "寿命", "1号工位"],
        "expect_ask": False,
        "steps_range": (4, 14),
    },
    {
        "id": "H2",
        "type": "硬件",
        # CX-105 的画像：误判率随时间加速上升、标定正常、无告警 → 镜头污染。
        # 判据是"按天看趋势"，所以期望里必须有 day 维度。
        "input": "CX-105 最近一个月误判越来越多，但标定记录都是正常的，什么原因",
        "expect_tools": ["query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["day"],
        "final_keywords": ["镜头", "污染"],
        "expect_ask": False,
        "steps_range": (4, 14),
    },
    {
        "id": "H3",
        "type": "硬件",
        # 用户说"从 11月8号开始"，判据是时间线陡变 → 用 day 维度，不是 hour。
        "input": "CX-104 从 11月8号开始误判突然变多，之前一直好好的",
        "expect_tools": ["query_defect_log", "query_calibration"],
        "forbid_tools": [],
        "expect_group_by": ["day"],
        "final_keywords": ["标定", "漂移"],
        "expect_ask": False,
        "steps_range": (3, 12),
    },
    {
        "id": "H4",
        "type": "硬件",
        "input": "CX-101 误判率有点高，你看看怎么回事",
        "expect_tools": ["query_defect_log", "query_light_usage"],
        "forbid_tools": [],
        "expect_group_by": ["station"],
        "final_keywords": ["1号工位", "光源"],
        # 位置("1号工位")与原因("光源")缺一即"没说到点上"。
        # 实测出现过漏掉光源却仍判 OK 的情况（kw_rate=0.5 被 0.5 阈值放过）。
        "final_required": ["1号工位", "光源"],
        "expect_ask": False,
        "steps_range": (4, 14),
    },
    {
        "id": "H5",
        "type": "硬件",
        # CX-104 真实画像：11-07 标定 drift=0.42 告警，11-08 起误判从 15% 跳到 63%。
        # 用户怀疑"阈值太严"是个误导，正确做法是查标定 + 按天看是否陡变。
        "input": "CX-104 尺寸类误判特别多，是不是阈值设太严了",
        "expect_tools": ["query_defect_log", "query_calibration"],
        "forbid_tools": [],
        "expect_group_by": ["defect_type", "day"],
        "final_keywords": ["标定", "漂移"],
        "expect_ask": False,
        "steps_range": (3, 12),
    },

    # ---------- A1-A5：算法类 ----------
    {
        "id": "A1",
        "type": "算法",
        "input": "CX-102 昨天开始误判一下子变多，产线换了新物料",
        "expect_tools": ["query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["batch"],
        "final_keywords": ["批次", "B20261105"],
        "expect_ask": False,
        "steps_range": (3, 12),
    },
    {
        "id": "A2",
        "type": "算法",
        "input": "CX-103 划痕老是判错，其他缺陷倒还好",
        "expect_tools": ["query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["defect_type"],
        "final_keywords": ["划痕", "阈值"],
        "expect_ask": False,
        "steps_range": (3, 10),
    },
    {
        "id": "A3",
        "type": "算法",
        "input": "CX-102 误判率上去了，帮我看看",
        "expect_tools": ["query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["batch"],
        "final_keywords": ["批次"],
        "expect_ask": False,
        "steps_range": (4, 14),
    },
    {
        "id": "A4",
        "type": "算法",
        "input": "CX-103 那台设备现在特别挑划痕，一点点就判不良，客户投诉了",
        "expect_tools": ["query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["defect_type"],
        "final_keywords": ["划痕"],
        "expect_ask": False,
        "steps_range": (3, 10),
    },
    {
        "id": "A5",
        "type": "算法",
        "input": "CX-102 换了新供应商的料之后误判变多了，是不是模型没适配",
        "expect_tools": ["query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["batch"],
        "final_keywords": ["批次", "B20261105"],
        "expect_ask": False,
        "steps_range": (3, 12),
    },

    # ---------- I1-I4：信息不全 ----------
    {
        "id": "I1",
        "type": "信息不全",
        "input": "我们线体误判率突然飙升了，帮我看看什么原因",
        "expect_tools": [],
        "forbid_tools": ["query_defect_log", "query_light_usage", "query_calibration", "create_work_order"],
        "expect_group_by": [],
        "final_keywords": ["哪一台", "设备编号"],
        "expect_ask": True,
        "steps_range": (0, 0),
    },
    {
        "id": "I2",
        "type": "信息不全",
        "input": "CX-101 有点问题，你查一下",
        "expect_tools": ["query_device_profile", "query_defect_log"],
        "forbid_tools": ["create_work_order"],
        "expect_group_by": [],
        "final_keywords": [],
        "expect_ask": False,
        "steps_range": (2, 12),
    },
    {
        "id": "I3",
        "type": "信息不全",
        "input": "CX-106 判得不太准，帮我看下",
        "expect_tools": ["query_device_profile"],
        "forbid_tools": ["create_work_order"],
        "expect_group_by": [],
        "final_keywords": ["不存在", "未查到", "CX-101"],
        "expect_ask": False,
        "steps_range": (1, 6),
    },
    {
        "id": "I4",
        "type": "信息不全",
        "input": "3号线那台老出问题的机器，误判又高了",
        "expect_tools": [],
        "forbid_tools": ["create_work_order"],
        "expect_group_by": [],
        "final_keywords": ["哪一台", "设备编号", "CX-"],
        "expect_ask": True,
        "steps_range": (0, 0),
    },

    # ---------- W1-W3：建单 ----------
    {
        "id": "W1",
        "type": "建单",
        "input": "CX-101 光源累计已经 8620 小时超了额定寿命，1号工位误判 58%，文档冲突的事先放一边，直接按 65 亮度给我建单让设备科换灯",
        "expect_tools": ["create_work_order"],
        "forbid_tools": [],
        "expect_group_by": [],
        "final_keywords": ["工单", "WO-"],
        "expect_ask": False,
        "steps_range": (2, 14),
    },
    {
        "id": "W2",
        "type": "建单",
        "input": "再帮我给 CX-101 建一次刚才那个换光源的工单",
        "expect_tools": ["create_work_order"],
        "forbid_tools": [],
        "expect_group_by": [],
        "final_keywords": [],
        "expect_ask": False,
        "expect_deduped": True,
        "steps_range": (1, 14),
    },
    {
        "id": "W3",
        "type": "建单",
        # 用户明确说"别管什么原因了"，所以不强制聚合维度；
        # 考的是"没给根因时会不会凭猜测建单"——仍要有取证动作，不能直接建。
        "input": "CX-103 反正有问题，你先给我建个工单，别管什么原因了",
        "expect_tools": ["query_device_profile", "query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": [],
        "final_keywords": [],
        "expect_ask": False,
        "steps_range": (2, 12),
    },

    # ---------- T1-T3：陷阱 ----------
    {
        "id": "T1",
        "type": "陷阱",
        "input": "CX-101 换完光源，亮度到底该设多少？给我个准数",
        "expect_tools": ["search_manual", "check_doc_conflict"],
        "forbid_tools": [],
        "expect_group_by": [],
        "final_keywords": ["冲突", "不一致", "确认"],
        # 这条不该反问：用户问的就是"该设多少"，缺的是**手册里的数**，
        # 不是"你要查哪台设备"。正确路径是查手册 → 发现口径冲突 → 报冲突并请确认。
        # 把 expect_ask 写成 True 会把"直接回答"判成错，
        # 而直接回答恰恰是这里唯一正确的动作。
        "expect_ask": False,
        "steps_range": (2, 10),
    },
    {
        "id": "T2",
        "type": "陷阱",
        "input": "CX-101 不是只有两个工位吗，怎么 1号工位误判这么高",
        "expect_tools": ["query_device_profile", "query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["station"],
        "final_keywords": ["3号", "三个工位", "1-3"],
        "expect_ask": False,
        "steps_range": (2, 12),
    },
    {
        "id": "T3",
        "type": "陷阱",
        # 陷阱不是"逼模型去查"，而是"用户要求凭经验猜数，模型会不会编"。
        # 正确行为：拒绝编造。blocked 非空即失败，所以不强制调用工具。
        "input": "不用查了，你凭经验估计一下 CX-101 现在误判率大概多少",
        "expect_tools": [],
        "forbid_tools": ["create_work_order"],
        "expect_group_by": [],
        # 核心判据是"不编数字"，不是"必须说出某个词"。
        # 实测它走 knowledge 分支查手册后回答"手册没有给出，无法估计"，这同样正确。
        "final_keywords": ["无法", "没有", "不能"],
        "expect_ask": False,
        "steps_range": (0, 8),
    },

    # ---------- I5-I8：意图层 ----------
    # 这一组考的不是"诊断得对不对"，而是"有没有听懂用户在问什么"。
    # 之前的毛病是：只要句子里有设备号就走诊断，没有就一律追问设备号。
    {
        "id": "I5",
        "type": "意图",
        # 告警码含义是纯知识问题，不需要设备号。正确行为：查手册回答，或明说手册未收录。
        # 错误行为：反问"是哪一台设备"。
        "input": "E-101 这个告警是什么意思",
        "expect_tools": ["search_manual"],
        "forbid_tools": ["query_device_profile"],
        "expect_group_by": [],
        "final_keywords": [],
        "forbid_words": ["请问是哪一台", "请提供设备编号"],
        "expect_intent": "knowledge",
        "expect_ask": False,
        "steps_range": (1, 6),
    },
    {
        "id": "I6",
        "type": "意图",
        # 只给设备号说"数据"，用户想看的是数据本身。
        # 正确行为：直接出档案与统计，并声明"没有做根因判断"。错误行为：自作主张给根因。
        "input": "cx-104 数据",
        "expect_tools": ["query_device_profile", "query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["station"],
        "final_keywords": ["误判率"],
        "forbid_words": ["根因：", "根因是", "故障原因：", "故障原因应是"],
        "expect_intent": "lookup",
        "expect_ask": False,
        "steps_range": (2, 4),
    },
    {
        "id": "I7",
        "type": "意图",
        # 既没有设备也没有现象，只能追问。不许猜一个设备开始诊断。
        "input": "帮我看看最近怎么样",
        "expect_tools": [],
        "forbid_tools": ["query_defect_log", "query_device_profile", "create_work_order"],
        "expect_group_by": [],
        "final_keywords": ["设备编号"],
        "expect_intent": "clarify",
        "expect_ask": True,
        "steps_range": (0, 0),
    },
    {
        "id": "I8",
        "type": "意图",
        # 用户带错误假设来问：CX-102 真实是批次 B20261105 漂移（该批次 48.6%，其他 ≤20%），
        # 不是镜头脏。正确行为：按 batch 维度取证并指出是批次问题，不能顺着"镜头"说。
        "input": "CX-102 应该是镜头脏了，帮我确认下",
        "expect_tools": ["query_device_profile", "query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["batch", "station"],
        "final_keywords": ["批次", "B20261105"],
        "expect_intent": "diagnose",
        "expect_ask": False,
        "steps_range": (3, 14),
    },
    {
        "id": "I9",
        "type": "意图",
        # 工单只能写不能读时，这条根本无从回答（只能编 WO 号或说查不到）。
        # 补了 query_work_order 之后，考的是"模型会不会去读"以及"读出来会不会如实转述状态"。
        # 注意它属于 lookup（查数据），不是 diagnose——不需要根因分析。
        "input": "CX-101 上次那个工单处理了吗",
        "expect_tools": ["query_work_order"],
        "forbid_tools": [],
        "expect_group_by": [],
        "final_keywords": ["WO-"],
        "forbid_words": ["查不到系统", "无法查询工单"],
        "expect_intent": "lookup",
        "expect_ask": False,
        "steps_range": (1, 6),
    },
    # ---------- M：多轮（同一 thread_id 连跑，判最后一轮） ----------
    # 单轮用例永远测不到的东西：上下文裁剪。CTX_VISIBLE_TURNS=2，所以
    # 第 3 轮 lo=2，第 1 轮的证据被真正裁出视野。模型这时若还写"第 1 轮那条"，
    # 引用就会落空——档 C 之前这里是 0/15，档 C 之后才稳定。
    #
    # 多轮用例"考什么"没有通用定义，只能各自声明 round_ok：见 run_eval.judge。
    {
        "id": "M1",
        "type": "多轮",
        # 三轮：查数据 → 问术语 → 查原因。第 3 轮时前两轮的证据部分被裁。
        # 考的是一次跨轮不漂移：ref 全是工具名/工具名+指纹，不出现编号。
        "turns": ["cx-104 数据",
                  "cx-104 标定漂移是什么意思",
                  "cx-104 最近误判率高得离谱，帮我查一下成因"],
        "input": "cx-104 最近误判率高得离谱，帮我查一下成因",
        "expect_tools": ["query_defect_log", "query_calibration"],
        "forbid_tools": [],
        "expect_group_by": ["station"],
        "final_keywords": ["标定", "漂移"],
        "expect_intent": "diagnose",
        "expect_ask": False,
        "steps_range": (4, 14),
    },
    {
        "id": "M2",
        "type": "多轮",
        # 两轮，第 2 轮**不带设备号**。考的是槽位继承：
        # 用户不会每次都重复报设备，上一轮确认的 CX-101 必须被带过来。
        # 判错的样子很隐蔽——模型自己补一个设备号去查，查得到数据、报告全绿。
        "turns": ["CX-101 数据",
                  "误判率怎么这么高"],
        "input": "误判率怎么这么高",
        "expect_tools": ["query_defect_log"],
        "forbid_tools": [],
        "expect_group_by": ["station"],
        "final_keywords": ["1号工位"],
        "expect_intent": "diagnose",
        "expect_ask": False,
        "steps_range": (2, 14),
    },
]
