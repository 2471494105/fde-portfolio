"""7 个工具的 function-calling schema（OpenAI / Ollama 通用格式）。

描述里写死取证要求，而不是靠提示词约束——W0 实测证明：提示词加硬约束会让
语义正确率从 40% 掉到 20%（指令过载）。把要求放进工具描述，模型选工具时就看到。
"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_device_profile",
            "description": (
                "查询**单台设备**的档案：产线、工位、相机/镜头/光源型号、安装日期、"
                "保养周期（这台当前设定的天数）、误判记录条数与最近一次误判时间。"
                "凡是问『我这台』的当前配置、实际取值、最近记录，都只能用它——"
                "手册里只有通用规程，没有某台设备实际怎么配、配了多少。"
                "历史标定记录与漂移量不在这里，用 query_calibration。"
                "在任何涉及具体设备的诊断开始时先调此工具确认设备存在。"
                "如果用户提到的设备编号查不到，不要假设它存在，直接说明未查到并列出可用设备。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号，例如 CX-101"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_defect_log",
            "description": (
                "查询某台设备的误判（过杀）日志并按维度聚合，是判断故障方向的主证据。"
                "取证要求：判断是硬件问题还是算法问题，必须看误判是否集中——"
                "集中在一个工位(station)说明该工位的镜头/光源/标定有问题，偏硬件；"
                "集中在某个物料批次(batch)或某个缺陷类型(defect_type)说明模型与参数是所有工位共用的，偏算法；"
                "按小时(hour)聚合用于看单日内的时段规律。"
                "只查一个维度就下结论属于取证不足，硬件类排查至少应再看工位分布。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号"},
                    "time_from": {
                        "type": "string",
                        "description": (
                            "起始时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。"
                            "留空只取最近 7 天——如果用户说的是一周以上（最近一个月、"
                            "这两个月、从某月某日起），必须把该起点写进 time_from，"
                            "否则趋势会被 7 天窗口截断，看到的只是平台期。"
                        ),
                    },
                    "time_to": {"type": "string", "description": "结束时间，留空则取数据最新时间"},
                    "group_by": {
                        "type": "string",
                        "enum": ["hour", "day", "station", "defect_type", "batch"],
                        "description": (
                            "聚合维度：hour 小时 / day 按天（看误判率随时间上升还是陡变，"
                            "判断缓慢漂移类故障必须用这个维度）/ station 工位 / "
                            "defect_type 缺陷类型 / batch 物料批次。"
                            "不知道选哪个时先查 station——误判最常见的形态是集中在单个工位，"
                            "而整体误判率看不出这一点。"
                        ),
                    },
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_calibration",
            "description": (
                "查询**单台设备**的历史标定记录：每次标定的时间、漂移量(px/mm)、结果、操作人。"
                "问『上次什么时候标定的』『最近漂移多大』用它；"
                "问『保养周期该是多少天』用 query_device_profile（那是这台设定的值）。"
                "误判率在某个时间点突然上升时，应查看该时间点前后的标定记录，"
                "确认是否存在标定漂移超阈值（阈值见手册，注意不同文档口径可能不一致）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号"},
                    "limit": {"type": "integer", "description": "返回最近几条，默认 5"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_light_usage",
            "description": (
                "查询光源累计点亮时长、额定寿命、当前亮度设定与亮度调整历史。"
                "如果误判集中在某个工位且夜间更重，或亮度被多次下调，应查此工具确认光源是否超寿命。"
                "返回里两个寿命字段方向相反、都非负：over_life_hours 是超出额定寿命的时长"
                "（没超就是 0），remaining_hours 是距额定寿命还剩的时长（已超就是 0）。"
                "说\"超了多少\"引前者，说\"还剩多少\"引后者。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_manual",
            "description": (
                "在设备手册/作业指导书中做语义检索，返回最相关片段及出处。"
                "只返回**通用规程与标准条文**，不含任何具体设备的实际记录、历史日期或当前取值："
                "查『CX-104 上次什么时候标定的』应该用 query_device_profile，"
                "本工具只会返回『规程要求每 90 天标定一次』这种通用条款。"
                "适合查通用设定值、判定阈值、处置步骤、条款含义。"
                "引用手册内容时必须注明来源文档与章节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索问题，例如 光源亮度设定标准值"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 3，最多 8"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_doc_conflict",
            "description": (
                "检测同一个参数在厂商手册、厂内SOP、保养计划、质量目标之间是否存在取值冲突。"
                "规则：只要发现不同文档对同一参数给出不同数值，就说明存在冲突，"
                "此时不要自行挑选一个值继续推理，必须把冲突上报给用户并请其确认口径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "param_name": {
                        "type": "string",
                        "description": "参数名，例如 光源亮度设定、标定周期(天)、判定置信度阈值",
                    },
                },
                "required": ["param_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_work_order",
            "description": (
                "查询历史工单（只读）。用户问『上次那个工单处理了吗』『这台设备建过单吗』"
                "『现在有哪些待处理的工单』时用它。不要凭印象回答，更不要编造工单号。"
                "返回的 state 字段区分三种情况：device_not_found（设备不在台账）、"
                "no_order（设备存在但没有任何工单）、found（查到工单）。"
                "查到 0 条时如实说『没有工单记录』，不要说成『系统查不到』。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "设备编号，例如 CX-101。不传则查全厂工单。",
                    },
                    "status": {
                        "type": "string",
                        "description": "工单状态精确匹配，例如 待处理。不传则不限状态。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10，上限 50。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_work_order",
            "description": (
                "创建维修工单并分派。只有在根因已确认、且已给出处置动作时才创建。"
                "同一设备同一故障分类如果已经有未关闭的工单，调用会返回那张已有单"
                "（deduped=true），不会重复建单；工单关闭之后再报同类故障，会正常建新单。"
                "建单前必须先完成取证，不能凭猜测建单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号"},
                    "root_cause": {"type": "string", "description": "根因，一句话，例如 光源超额定寿命导致亮度衰减"},
                    "root_cause_code": {
                        "type": "string",
                        "enum": [
                            "light_degradation",
                            "lens_contamination",
                            "calibration_drift",
                            "batch_drift",
                            "threshold_misconfig",
                            "other",
                        ],
                        "description": (
                            "根因分类，用于幂等去重：同一台设备同一分类，"
                            "同时只允许一张未关闭的工单（关闭后可再建）。"
                            "light_degradation 光源衰减/超寿命；lens_contamination 镜头污染；"
                            "calibration_drift 标定漂移；batch_drift 物料批次漂移；"
                            "threshold_misconfig 判定阈值不当；other 其他"
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "high 需停机/设备科介入；medium 需工艺或算法调整；low 班组自处理",
                    },
                    "actions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "处置动作列表，每条一句话",
                    },
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "支撑该根因的证据引用，例如 1号工位误判率57.6%、光源累计8620h",
                    },
                },
                "required": ["device_id", "root_cause", "root_cause_code", "severity", "actions", "evidence_refs"],
            },
        },
    },
]
