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
                # ── 时间线：必查维度，不是可选维度 ───────────────────────────
                "**时间线是必查维度，别当可选项。**只要用户的话里出现"
                "『最近变多了』『从上周开始』『某天之后就明显不对』这类"
                "**从某个时点起发生变化**的描述，就先按 day 看按天趋势，"
                "再谈原因。缓慢漂移和阶跃突变在总量上看不出来，在 day 维度上一眼就是拐点；"
                "而默认 7 天窗口可能把拐点截在窗外，所以这种情况要同时把 time_from 往前推。"
                "（CX-104 实测：按总量看是『四类缺陷都高、两工位都高』，"
                "按 day 看才发现 11-07 是 15.4%、11-08 起跳到 63.2%。）"
                # ── 用户的前提不是证据 ──────────────────────────────────────
                "**用户带来的猜测不构成证据。**『是不是阈值设太严』『是不是镜头脏了』"
                "只说明他在怀疑什么，不说明数据支持什么。只要整体误判偏高而原因不明，"
                "就先 day 看时间线 + query_calibration 看标定漂移，再看算法侧参数；"
                "不要因为用户的猜测落在算法侧，就把时间维度和标定记录跳过——"
                "那正是被他的前提框住、停止取证的过程。"
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
                        "description": (
                            "聚合维度，可取：hour 小时 / day 按天（看误判率随时间上升还是陡变，"
                            "判断缓慢漂移类故障必须用这个维度）/ station 工位 / "
                            "defect_type 缺陷类型 / batch 物料批次。"
                            "**可以一次传多个维度，用英文逗号连接，例如 station,batch**——"
                            "一次调用会把每个维度分别聚合、分别返回，"
                            "不要为了看几个维度就分几次调用。"
                            "要换时间窗重看同一批维度时，也请把多个维度一次带上。"
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
                "误判率在某个时间点突然上升时，用 time_from / time_to 圈出那段时间再看标定记录，"
                "不要靠 limit 猜——limit 只按时间倒序取最近 N 条，圈不出「那一天前后」。"
                "确认是否存在标定漂移超阈值（阈值见手册，注意不同文档口径可能不一致）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备编号"},
                    "limit": {"type": "integer", "description": "返回最近几条，默认 5；有 time_from/time_to 时是窗口内的最近几条"},
                    "time_from": {
                        "type": "string",
                        "description": "只看该时间之后（含）的标定，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。不传则不限下界。",
                    },
                    "time_to": {
                        "type": "string",
                        "description": "只看该时间之前（含）的标定。不传则不限上界。",
                    },
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
                "一次可查多个参数（param_name 用英文逗号连接），结果按参数放在 by_param 里；"
                "不要为每个参数单独调用一次，那会把同一个判断拆成多次噪声。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "param_name": {
                        "type": "string",
                        "description": (
                            "参数名，例如 光源亮度设定、标定周期(天)、判定置信度阈值。"
                            "多个参数用英文逗号连接一次查完（如「光源亮度设定,标定周期(天)」）。"
                        ),
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
                "返回里恒带 scope 字段，写明本次查询的范围（device:CX-101 或 全厂(status=待处理））。"
                "引用工单时必须说明范围，不要把全厂结果当成某台设备的。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": (
                            "设备编号，例如 CX-101。查单台设备就传它。"
                            "不传时必须同时给 status，否则调用会被拒绝（bad_args）——"
                            "两个都不给等于拖全厂整张工单表。"
                        ),
                    },
                    "status": {
                        "type": "string",
                        "description": (
                            "工单状态精确匹配，例如 待处理。不传则不限状态；"
                            "但 device_id 也不传时它是必填项。"
                        ),
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
                "创建维修工单并分派。两种情形都必须建单，不要自行降级成只做诊断："
                "①根因已确认、已给出处置动作时，按根因建单；"
                "②用户明确要求建单、但根因尚未确认时（例如「先给我建个工单，别管什么原因」），"
                "照样建——root_cause 如实写「用户要求派工，根因待查」，"
                "不要因为「根因没确认」而拒绝，用户是派工人，不是审批对象。"
                "同一设备同一故障分类如果已经有未关闭的工单，调用会返回那张已有单"
                "（deduped=true），不会重复建单；工单关闭之后再报同类故障，会正常建新单。"
                "除了上一种「用户明确要求」的情形，建单前要先完成取证，不能凭猜测编根因。"
                "成功后返回的 order_id 必须原样出现在最终结论里（conclusion 或 advice），"
                "只说「已建单」而不写单号，用户就查不到这张单，等于没办成。"
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
                        "description": (
                            "支撑该根因的证据引用。每条必须是一个可解析的指针，"
                            "格式「工具名#字段路径」；同一工具在一轮里调过多次时，"
                            "写成「工具名{参数名=值}#字段路径」以区分是哪一次调用。"
                            "例如：query_defect_log{group_by=station}#groups[0].rate。"
                            "不要写「1号工位误判率57.6%」这类自然语言复述——那样的引用"
                            "无法被程序核对，等于没有证据。"
                        ),
                    },
                },
                "required": ["device_id", "root_cause", "root_cause_code", "severity", "actions", "evidence_refs"],
            },
        },
    },
]
