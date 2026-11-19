"""跑 20 条 golden path 并给四类指标打分。

四指标：
- 工具选对率：期望工具出现了几个
- 聚合维度覆盖：期望的 group_by 是否在调用参数里出现过（any 语义，不看顺序）
- 终局正确率：结论关键词命中率
- fc 失败率：工具调用参数解析失败 / 未知工具的次数

语义正确 = 工具命中、维度命中、终局命中、未越禁用工具、步数在区间、
无伪引用、无 fc 失败、幂等成立（或环境不可达标灰）、意图判对、该反问时反问、
无禁用表述、结论落点齐全、多轮引用成立。

注意三个"标灰不判死"的口径，它们都不是失败：
- `ref` 写 null（有未溯源数字）——区分不了"我算的"和"我编的"
- 建单工具连不上（幂等未验证）——那是环境没起，不是模型没做
- 证据超长被截断（cite_cut）——返回体装不下，无从核验
标灰的意思是"这项判不了"，不是"这项通过了"，所以报告里各有独立一行。
"""

import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import run  # noqa: E402
from app.graph.llm import BASE_URL, MODEL, PROVIDER  # noqa: E402
# 停止原因的取值只此一份，评测这里直接引用常量，不再手抄字符串。
# 手抄一次就多一份可以各自漂移的副本——这正是"同一份事实存两份"最常见的起手式。
from app.graph.nodes import CONF_OK, STOP_BUDGET_EXHAUSTED  # noqa: E402
from app.meta import code_hash as _code_hash, git_commit as _git_commit  # noqa: E402
from eval.trajectories import TRAJECTORIES  # noqa: E402


def _cases_hash():
    """用例集指纹：改了期望值，旧报告的分数就不再可比。"""
    try:
        blob = json.dumps(TRAJECTORIES, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return ""


def _used_group_by(state):
    """这一轮用过哪些聚合维度。

    2026-11-19 起 group_by 允许逗号分隔的多维（"station,batch"）。原来整串塞进
    set，多维时要么整串 ≠ 期望单维度、要么不可哈希，会漏判；这里按逗号拆开，
    同时兼容数组形式。
    """
    cols = []
    for e in state.get("evidence", []):
        gb = e["args"].get("group_by") if isinstance(e["args"], dict) else None
        if isinstance(gb, str):
            cols.extend(x.strip() for x in gb.split(",") if x.strip())
        elif isinstance(gb, (list, tuple)):
            cols.extend(str(x).strip() for x in gb if str(x).strip())
    return set(cols)


def _norm(text):
    """去掉所有空白（含全角空格）再比较。

    模型输出会出现"1 号工位"（数字与汉字之间带空格）、"1号工位"、
    "1＃工位"等写法差异。按字面比会被一个空格打败，把正确结论判成失败——
    这是测量工具的缺陷，不是模型的问题。
    """
    return re.sub(r"\s+", "", text or "")


def _retry_after(text):
    """从 429 消息里抠出服务端建议的等待秒数。

    中转站会明说「请求太频繁，请 11 秒后再试」，中英两版都出现过。
    照它说的等，比拍一个固定值稳：拍短了刚睡醒又撞，拍长了整份评测白等。
    抠不到就退 12 秒——比常见的最小间隔略长。上限 60 秒，
    避免一次异常的报错把整轮评测卡住。
    """
    pats = (r"(\d+(?:\.\d+)?)\s*秒后再试", r"retry in (\d+(?:\.\d+)?)\s*second")
    for p in pats:
        m = re.search(p, text or "", re.I)
        if m:
            return min(float(m.group(1)) + 2.0, 60.0)
    return 12.0


def _result_dict(raw):
    """把 evidence 里那条压缩文本还原成 dict。

    为什么不能直接当 dict 用：act 存进 evidence 的是 _payload 压过的**文本**
    （"[query_work_order{device_id=CX-101}] {json...}"），不是工具的原始返回对象。

    原来的幂等判据写的是 `isinstance(e["result"], dict)`，对每一条都不成立——
    `orders` 永远为空、`deduped_ok` 永远 False。W2 从这条判据写下那天起就注定 FAIL，
    与模型做没做幂等无关。这比"模型做错了"更隐蔽：报告上它长得像一个稳定的失败用例。

    再往下，`fc_fail` 也用了同一个 dict 判断，所以"工具调用格式错"这个指标
    也一次都没真正生效过——一直显示 0 不代表没发生，代表没测。
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    # 前缀是 "[工具名]" 或 "[工具名{参数指纹}]"，返回体在最后一段花括号里。
    # 指纹本身也带花括号，所以从右往左试，第一个能解析成功的才是返回体。
    for i in reversed([i for i, ch in enumerate(raw) if ch == "{"]):
        try:
            obj = json.loads(raw[i:])
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _steps_ok(lo, hi, steps):
    """步数是否落在期望区间内。

    hi=None 表示"不判上界"，不是"不限"：预算由 MAX_STEPS 与
    stop_reason=budget_exhausted 管。steps 按工具调用次数累加（一次 act
    可能 +N），本来就可能冲过预算；那一次冲过头是 budget_exhausted 该报的
    事，步骤区间再报一遍会让报告自相矛盾（steps_ok 绿、预算耗尽红）。
    上界写具体数字的用例问的是另一件事：这道题本该这么快，超了就是绕路。

    抽成模块级函数是为了让判据与测试同源——测试复刻一遍表达式，
    改的时候漏改一边，测出来的就不是线上跑的那条判据。
    """
    return lo <= steps and (hi is None or steps <= hi)


def judge(traj, state):
    tools = state.get("tool_names", [])
    final = state.get("final", "") or ""
    # 用渲染后的可读版本判断关键词：final 现在是 claims JSON，
    # 直接匹配会把字段名（"conclusion"）也算进去，等于放宽了关键词检查。
    readable = state.get("final_text") or final
    steps = state.get("steps", 0)
    blocked = state.get("blocked", [])
    # 伪引用单独判死：ref 指了一次真实发生的调用，但那次的返回里没有这个数。
    # blocked 里的其余值（没写出处、数出来程序认不出）标灰放行——
    # 它们区分不了"我算的"和"我编的"，判死等于把合法的派生分析一起杀掉。
    misquoted = state.get("misquoted", [])
    # 溯源达标 = 没有伪引用（引了字段、但字段撑不住这个数）。
    # ref 写 null 不算不达标：那是"没有出处的数字"，标灰放行即可，
    # 拿它判失败会让"模型诚实标注"反而变成减分项。
    trace_ok = not misquoted
    # 校验层的判定明细一并入报告。之前只记 blocked 的字符串，
    # 想知道"路径锚点写对了几次"就得拿 evidence 离线重算一遍，
    # 等于每次复盘都要临时写脚本——这一层留档就断了。
    cite_stats = state.get("cite_stats") or {}
    # 结束原因：need_more / enough / budget_exhausted。
    # 只看 steps 分不出"扫满 12 步还是没结论"和"12 步里自己判断够了"——
    # 前者是半成品，后者是可交付结论，可信度差着量级。
    stop_reason = state.get("stop_reason", "")
    # check 闸口回环过几次：0 = 一次就过，1 = 被拦下后改才对。
    # 和 misquote_n 必须分开看——只看"没失败"会把"一次就干净"和"改过一次才对"
    # 混成同一个"没问题"，而后者本身就是模型稳定性的一个指标。
    check_rounds = int(state.get("check_retry") or 0)

    expect = traj["expect_tools"]
    hit = [t for t in expect if t in tools]
    tool_rate = (len(hit) / len(expect)) if expect else 1.0

    forbid_hit = [t for t in traj.get("forbid_tools", []) if t in tools]

    gb_expect = traj.get("expect_group_by", [])
    gb_used = _used_group_by(state)
    gb_hit = [g for g in gb_expect if g in gb_used]
    gb_rate = (len(gb_hit) / len(gb_expect)) if gb_expect else 1.0

    kws = traj.get("final_keywords", [])
    flat = _norm(readable)
    kw_hit = [k for k in kws if _norm(k) in flat]
    # 没设关键词 ≠ 命中率 100%。原来这里 `if kws else 1.0`，于是 I2/W2/W3/I5
    # 这四条"判据根本没验过结论"的用例，在报表上和"关键词全中"长得一模一样：
    # 既拉高了平均值，又让人以为它们被验过。
    # 改成 None —— 不判失败（判据没设条件不等于模型答错，和标灰同一立场），
    # 但不进命中率的分母，并在汇总里单列条数。
    kw_na = not kws
    kw_rate = (len(kw_hit) / len(kws)) if kws else None

    # 结论层有两条独立的证据通道，不是"对/错"二选一：
    #   1) 说对了词——kw_rate
    #   2) 产生了对的结构事实——unknown 里记的"查过了确实没有"
    #
    # 为什么必须加第二条：同一份输入跑两次，模型会把"查不到"换成"没有"
    # （实测 I3 两次措辞不同），拿措辞当判据等于让判据跟着模型的口语漂。
    # Claude Citations 的做法是 cited_text 由 API 从原文填、模型只给位置——
    # 判据的真值来自系统，不来自模型的自由表达。这里同理：
    # 设备到底在不在台账，是 query_device_profile 的返回决定的，不是模型怎么措辞决定的。
    unk_kinds = {u.get("kind") for u in (state.get("unknown") or []) if isinstance(u, dict)}
    unk_expect = traj.get("expect_unknown", [])
    unk_hit = [k for k in unk_expect if k in unk_kinds]
    unknown_ok = bool(unk_expect) and len(unk_hit) == len(unk_expect)

    # 半成品：模型调用挂了，结论是占位文案。
    # 归 env 不归 model——依据失败归因三分类（Task's Fault / Agent's Fault /
    # Infrastructure Failure）与 Anthropic《Quantifying infrastructure noise》：
    # 基础设施故障与"任务-模型"这条轴正交，混进模型失败率会让报表失真。
    svc_fail_n = int((cite_stats or {}).get("svc_fail") or 0)

    # 结论达标 = 说对了词，或产生了对的结构事实。
    # 写成"或"而不是给 I3 开特例：特例是"这条用例单独放行"，
    # 而"查无"是一整类结论形态，任何一条问不存在对象的用例都该走这条路。
    # kw_rate 为 None 表示这条用例没设关键词，判据管不到结论：
    # 按"不判失败"处理（缺判据不是模型的错），但这不代表"答得好"，
    # 所以它既不进命中率分母，也单独计数。
    conclusion_ok = (kw_rate is None) or (kw_rate >= 0.5) or unknown_ok

    lo, hi = traj["steps_range"]
    steps_ok = _steps_ok(lo, hi, steps)

    # evidence 里存的是文本不是 dict，原来那个 isinstance 判断让它永远不成立，
    # 于是"工具调用格式错"这个指标一直是 0——不是没发生，是没测。
    fc_fail = any(
        ("参数 JSON 解析失败" in str(e.get("result"))
         or "未知工具" in str(e.get("result")))
        for e in state.get("evidence", [])
    )

    # 幂等验证：直接看建单工具返回的 deduped 字段，比查结论关键词准。
    # 但"建单没成功"有两种原因，判据不能只认一个好结果：
    #   环境不可达（mock QMS 没起）—— 评测环境的问题，判不了模型对错
    #   连上了但没去重         —— 这才是模型/业务的问题
    # 只找 order_id + deduped 的话，第一种会被报成"未命中幂等"，
    # 而 W2 的实际情形正是这样：模型发现 QMS 挂了、如实报告、
    # 还指出"已有两张同类工单不必重复新建"，行为完全正确却被判失败。
    #
    # 三态而不是"只认 True"：写成 expect_deduped=False 却什么都不判，
    # 等于配置写了、判据不执行——那正是单向评测的入口
    # （Anthropic《Demystifying evals for AI agents》："Test both the cases where
    # a behavior *should* occur and where it *shouldn't*. One-sided evals create
    # one-sided optimization."）。当前用例里只有 W1 的第二轮用 True，它靠
    # "库里已有第一轮那张单"证明第一轮真的建了新单；False 留给"要求真建新单"的场景。
    deduped_ok = True
    deduped_env = ""
    if "expect_deduped" in traj:
        want = traj["expect_deduped"]
        results = [
            _result_dict(e.get("result")) for e in state.get("evidence", [])
            if (_result_dict(e.get("result")) or {}).get("order_id")
        ]
        errs = [
            (_result_dict(e.get("result")) or {}).get("error")
            for e in state.get("evidence", [])
            if (_result_dict(e.get("result")) or {}).get("error")
        ]
        if not results and errs:
            # 一次都没连上：幂等这项无从验证。标灰——不计失败，也不冒充通过。
            deduped_env = str(errs[-1])[:120]
        else:
            # 取最后一次建单的返回。没有建单记录就是 None——既不是"新建"
            # 也不是"去重"，两种期望都该失败。
            got = results[-1].get("deduped") if results else None
            deduped_ok = got is want

    # 意图层：考的是"有没有听懂用户在问什么"，不是"诊断得对不对"
    intent_expect = traj.get("expect_intent")
    intent_ok = True
    if intent_expect:
        intent_ok = state.get("intent") == intent_expect

    # 「该不该反问」也得判，不能只写在用例里。
    # `expect_ask` 27 条用例全都有这个字段、judge 却从来没读过——
    # 于是 I1/I4/I7 三条"信息不全"用例是**碰巧过**的：它们不期望工具、
    # 也没有关键词，模型哪怕自己猜一台设备去查，照样判 OK。
    # 假绿灯就是这样长出来的：期望写下了，但没有任何一行代码去兑现它。
    # 判据用意图而不是字面：该反问时 intent 必须落到 clarify。
    ask_expect = traj.get("expect_ask")
    ask_ok = True
    ask_detail = ""
    if ask_expect:
        ask_ok = state.get("intent") == "clarify"
        if not ask_ok:
            ask_detail = f"该反问却直接作答(intent={state.get('intent') or '空'})"

    forbid_words = [w for w in traj.get("forbid_words", []) if _norm(w) in flat]

    # final_required：必须全部出现的关键词，为空则不检查（保持老用例行为不变）
    # 与 final_keywords 的区别：final_keywords 是"同义任选"，阈值 0.5；
    # final_required 是"结论必须落在这些点上"，缺一即判失败。
    req = traj.get("final_required", [])
    req_miss = [w for w in req if _norm(w) not in flat]

    # 多轮用例：单轮那套判据看不出"跨轮还准不准"。第 3 轮真正要考的是——
    # 模型引的每一处出处，在**它这一轮还能看到的证据里**都要成立。
    # cite_oob（该工具的返回已被裁出）与 path_miss（编了字段）都是引用落空，
    # 只是成因不同；ref_bad 是把编号写回来。三个都必须为 0 才算 round_ok。
    # 不设这个判据的话，多轮用例和单轮用例考的是同一件事，"进 eval"就白进了。
    round_ok = True
    round_detail = ""
    if traj.get("turns"):
        cs = cite_stats or {}
        dead = int(cs.get("cite_oob") or 0) + int(cs.get("path_miss") or 0) \
            + int(cs.get("ref_bad") or 0)
        round_ok = dead == 0
        round_detail = (f"引用落空 {dead} 条"
                        f"（oob={cs.get('cite_oob') or 0}"
                        f" miss={cs.get('path_miss') or 0}"
                        f" bad={cs.get('ref_bad') or 0}）")

    semantic_ok = (
        tool_rate >= 1.0
        and gb_rate >= 1.0
        and conclusion_ok
        and not svc_fail_n
        and not forbid_hit
        and steps_ok
        and not misquoted
        and not fc_fail
        and deduped_ok
        and intent_ok
        and ask_ok
        and not forbid_words
        and not req_miss
        and round_ok
    )

    # 归因：失败落在哪一类。
    #
    # reasons 是给人看的一句话；failure 是同源的机器可读版本。
    # 为什么要多这一层：reasons 一直是一个字符串常量，于是"追问 / 没答 /
    # 答偏 / 抗令"这些完全不同的病，只要都表现为"关键词没对上"，就全被
    # 压成同一句"判断错/结论未命中"。看报告的人得自己拿 kw_rate、tools、
    # ask_ok 重新组合一遍——而组合规则是固定的，固定规则就该写成代码。
    #
    # 两者同源产出，不是为了少写一遍：分开写迟早漂移，一处改了另一处忘。
    reasons = []
    failure = []

    def _fail(code, owner, msg, note="", **basis):
        reasons.append(msg)
        item = {"code": code, "owner": owner, "basis": basis}
        if note:
            item["note"] = note
        failure.append(item)

    if not intent_ok:
        _fail("intent_wrong", "model",
              f"意图判错({state.get('intent') or '空'}，应为{intent_expect})",
              got=state.get("intent") or "空", expect=intent_expect)
    if not ask_ok:
        _fail("no_clarify", "model", ask_detail, intent=state.get("intent"))
    if forbid_words:
        _fail("forbid_word", "model",
              f"出现禁用表述({','.join(forbid_words)})", words=forbid_words)
    if tool_rate < 1.0:
        _fail("tool_missing", "model",
              f"工具选错(缺{','.join(t for t in expect if t not in tools)})",
              missing=[t for t in expect if t not in tools])
    if gb_rate < 1.0:
        _fail("gb_missing", "model",
              f"取证维度不足(缺{','.join(g for g in gb_expect if g not in gb_used)})",
              missing=[g for g in gb_expect if g not in gb_used])
    # 半成品先单独记：它同时也必然 kw_rate=0，
    # 若不在这里拦一道，一次 429 会被记成"模型结论未命中"，
    # 等于把基础设施故障再算一遍到模型头上。
    if svc_fail_n:
        _fail("svc_fail", "env",
              "整理答案时模型调用失败（结论是半成品，应重跑，不计入模型能力）",
              svc_fail=cite_stats.get("svc_fail"))
    if not conclusion_ok and not svc_fail_n:
        # 原来这一条只有一句"判断错/结论未命中"，至少盖住三种：
        #   追问了但该直接答 → 没答，病根在"该不该反问"
        #   调了工具仍没命中 → 答偏，病根在结论
        #   什么都没有     → 没答也没查
        # 判据分得出前两种就分开记；分不出的写进 note，不假装判得出来。
        if state.get("intent") == "clarify" and not ask_expect:
            _fail("asked_instead", "model",
                  "判断错/结论未命中（追问了，但这条用例要求直接作答）",
                  kw_rate=kw_rate, intent="clarify")
        elif tools:
            _fail("conclusion_off", "model",
                  f"判断错/结论未命中（调了 {len(tools)} 个工具，结论仍没落在关键词上）",
                  kw_rate=kw_rate, tools=tools, kw_hit=kw_hit,
                  # 有 forbid_tools 不等于"禁止调用"：T3 只禁了 create_work_order，
                  # 其余工具照调不违规。真正说明"这条用例没要求任何工具"的是
                  # expect_tools 为空——那种情况下调了工具算不算多余，判据说不准。
                  note=("这条用例没要求任何工具，判据看不出这些调用是多余还是有用"
                        if not expect else ""))
        else:
            _fail("conclusion_miss", "model", "判断错/结论未命中",
                  kw_rate=kw_rate, kw_hit=kw_hit)
    if req_miss:
        _fail("req_miss", "model", f"结论落点不全(缺{','.join(req_miss)})",
              missing=req_miss)
    if forbid_hit:
        _fail("tool_forbidden", "model", f"越界调用({','.join(forbid_hit)})",
              hit=forbid_hit)
    if not steps_ok:
        # 步数越界多半是用例区间写窄了，也可能是模型真绕远路——判据手里
        # 只有这一个数，分不出来。owner 记 uncertain，不默认怪模型。
        _fail("steps_out", "uncertain", f"步数越界({steps}不在{lo}-{hi})",
              steps=steps, lo=lo, hi=hi)
    if misquoted:
        _fail("cite_wrong", "model", f"引用写错({','.join(misquoted)})",
              misquoted=[m[:80] for m in misquoted[:5]])
    if fc_fail:
        _fail("fc_bad", "model", "工具调用格式错")
    if not deduped_ok:
        _fail("idem_miss", "model", "未命中幂等（应返回已存在工单）")
    if not round_ok:
        _fail("round_cite_dead", "model", round_detail)

    return {
        "id": traj["id"],
        "type": traj["type"],
        "intent": state.get("intent", ""),
        "intent_ok": intent_ok,
        # 该反问时有没有反问。和 intent_ok 分开记：意图判对但懒得追问，
        # 和意图就判错，是两种不同的毛病。
        "ask_ok": ask_ok,
        # 建单工具连不上时这里会填上错误原文，表示"幂等这项没验证"。
        # 空字符串 = 真的验证过（通过或没通过）。混在一起就分不出
        # "没去重"和"没连上"，而这两种要修的东西完全不同。
        "deduped_env": deduped_env,
        # 低置信不是判错：它是模型主动报的"我不确定"，用户会看到一行
        # "按「X」理解"或被改成追问。判错看 intent_ok，两个口径别混——
        # 混在一起会让"模型诚实"和"模型判错"变成同一个数字。
        "intent_conf": state.get("intent_conf"),
        # 这一轮的路由来源：rule:<规则名> / model。
        # 和 intent_conf 分开：conf 是模型自报，route_by 是实际生效的那条判断。
        "route_by": state.get("route_by") or "",
        # 模型自己判的那个。和 route_by 成对才有意义：
        # 只有 route_by 能说"规则命中了 8 条"，加上它才能说这 8 条里
        # 几条是模型会判错、被规则纠正的（救人），几条是规则自己也判错的（误伤）。
        "model_intent": state.get("model_intent") or "",
        # 期望意图也进报告：规则好坏要与它比，不该在汇总时再去回查夹具。
        "expect_intent": intent_expect or "",
        "tools": tools,
        "steps": steps,
        "stop_reason": stop_reason,
        "check_rounds": check_rounds,
        "tool_rate": round(tool_rate, 3),
        "gb_rate": round(gb_rate, 3),
        "kw_rate": (round(kw_rate, 3) if kw_rate is not None else None),
        "kw_na": kw_na,
        "kw_hit": kw_hit,
        "req_miss": req_miss,
        "steps_ok": steps_ok,
        "blocked": blocked,
        "misquoted": misquoted,
        "trace_ok": trace_ok,
        "fc_fail": fc_fail,
        "semantic_ok": semantic_ok,
        "round_ok": round_ok,
        "round_detail": round_detail,
        "turns": len(traj.get("turns") or []),
        "reasons": reasons,
        # 和 reasons 一一对应，带 code/owner/basis/note。
        # owner 只标判据真能判的三类：model（行为）/ uncertain（判据分不出）/
        # case（用例写错）。没有 evaluator——那是判据自己，判据不该给自己的错开脱。
        "failure": failure,
        "cite_stats": cite_stats,
        "final_head": readable[:160].replace("\n", " "),
    }


def _fixture_preflight():
    """跑全量之前先验判据没退化。返回 (失败用例 id 列表, 夹具总数)。

    为什么放在这里：一次全量 3000 秒。如果判据自己坏了，这 3000 秒算出来的
    每个数字都不可信——更糟的是它们仍然看起来像"模型能力"，会被当结论用。
    夹具 10 条、不到 0.1 秒，是这条链上最便宜的一次拦截。

    不算"跑评测"，只读本地文件、不连库不发请求，所以不会污染 meta。
    """
    from eval.check_criteria import FIXTURE, check

    if not FIXTURE.exists():
        return None, 0
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    return [c["id"] for c in cases if check(c)[0]], len(cases)


def main():
    only = None
    workers = 1
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = {x.strip().upper() for x in arg.split("=", 1)[1].split(",")}
        elif arg.startswith("--workers="):
            workers = max(1, int(arg.split("=", 1)[1]))

    targets = [t for t in TRAJECTORIES if not only or t["id"] in only]
    # --only 写了不存在的编号时，过滤结果是空集，原来只打一行"待评测 0 条"就跑完了：
    # 不报错、不写报告。写错一个字母和一个拼错的用例名，在这套输出里长得一模一样，
    # 而人的记忆是"我跑过它了"。宁可炸，也别让人以为跑过了。
    if only and not targets:
        known = sorted(t["id"] for t in TRAJECTORIES)
        print(f"!! --only={','.join(sorted(only))} 没有匹配任何用例")
        print(f"   现有 {len(known)} 条：{', '.join(known)}")
        return 2
    print(f"待评测 {len(targets)} 条，并发 {workers}\n")

    # 判据自检必须在这里拦，不能放到跑完之后——判据坏了，后面每个数字都是废的。
    # --no-fixture-check 是逃生口，但绕过要留痕：输出里那行 !! 会一起进报告。
    if "--no-fixture-check" in sys.argv[1:]:
        print("!! 绕过了判据自检：本次数字不能和带自检的那一轮直接比较\n")
    else:
        bad_ids, n_cases = _fixture_preflight()
        if bad_ids is None:
            print("!! 判据夹具缺失（eval/fixtures/criteria_cases.json 应该已提交）"
                  "——跳过自检，但这不正常\n")
        elif bad_ids:
            print(f"!! 判据退化：{', '.join(bad_ids)} 共 {len(bad_ids)} 条 FAIL。")
            print("   先跑：python eval/check_criteria.py -v")
            print("   改完判据再跑全量——这 3000 秒现在跑出来也是废的。\n")
            sys.exit(1)
        else:
            print(f"判据自检通过（{n_cases} 条夹具）\n")

    # 这一份 meta 是"这份报告是在什么条件下产生的"的唯一凭证。
    # 没有它，8 份历史报告之间无法比较：不知道换过模型、改过用例、还是改过 prompt。
    meta = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "commit": _git_commit(),
        "provider": PROVIDER,
        "model": MODEL,
        "base_url": BASE_URL,
        "cases_hash": _cases_hash(),
        "code_hash": _code_hash(),
        "cases_total": len(TRAJECTORIES),
        "evaluated": len(targets),
        "workers": workers,
        "use_checkpoint": workers == 1,
    }
    print(
        f"基线 commit={meta['commit'] or '(无)'}  model={MODEL}  "
        f"code_hash={meta['code_hash']}  cases_hash={meta['cases_hash']}\n"
    )

    # 建单类共享 mock QMS 的外部状态（同一张 work_orders 表）：并发跑会让
    # 这些用例的建单结果彼此交叉，幂等判定不可信。多轮类也必须串行，但原因
    # 不同：它们要共用 checkpointer（见 one()），而 checkpointer 是进程内
    # 单例、只有一条连接，并发写会直接报 "another command is already in
    # progress"。W1 同时命中两类——它是一条"建单 + 再建一次应去重"的多轮用例。
    # 表本身在每个全量开始前由 mock_qms/reset.py 清空，保证这次跑的起点确定。
    serial = [t for t in targets if t["type"] == "建单" or t.get("turns")]
    parallel = [t for t in targets if not (t["type"] == "建单" or t.get("turns"))]

    # 并发必须关掉 checkpointer：它是进程内单例、只有一条 psycopg 连接
    use_ckpt = (workers == 1)
    lock = threading.Lock()
    got = {}

    def one(traj):
        # 429 是中转站限流，不是模型的问题，直接判失败会把"跑法"记成"能力"。
        # I2/T2 就被这样冤枉过一次：同一份代码，串行补跑就对了。
        # 只重试这一类错误、且只重试一次——超时、JSON 错该判就判，
        # 无上限重试会让"平均耗时"这个数变成假的。
        for attempt in (0, 1):
            try:
                turns = traj.get("turns")
                if turns:
                    # 多轮必须共用 checkpointer：use_checkpoint=False 时 build_graph
                    # 每轮都新建一张图，上一轮的 messages / device_ids 全丢，
                    # 第 2 轮等于从零开始——那样"多轮用例"实际测的还是单轮，
                    # 而这正是它要防的那个假绿灯。
                    tid = f"eval-{traj['id']}-{int(time.time() * 1000)}"
                    state = None
                    for t in turns:
                        state = run(t, thread_id=tid, trace_run=False,
                                    use_checkpoint=True)
                else:
                    state = run(traj["input"], trace_run=False, use_checkpoint=use_ckpt)
                return traj, state, judge(traj, state)
            except Exception as exc:
                name, msg = type(exc).__name__, str(exc)
                is_429 = "RateLimitError" in name or "429" in msg
                if attempt == 0 and is_429:
                    time.sleep(_retry_after(msg))
                    continue
                return traj, None, {
                    "id": traj["id"], "type": traj["type"], "semantic_ok": False,
                    "intent_ok": True, "ask_ok": True, "deduped_env": "",
                    "reasons": [f"运行异常:{name}: {msg}"],
                    # 运行异常归 env，不归 model：超时、连不上、429 重试耗尽
                    # 都是评测环境的问题，判据不知道模型本来会不会做对。
                    # 这和 deduped_env 是同一个立场。
                    "failure": [{"code": "run_error", "owner": "env",
                                 "basis": {"case": name},
                                 "hint": f"运行异常:{name}: {msg}"}],
                    "tools": [], "steps": 0, "tool_rate": 0, "gb_rate": 0,
                    # 跑挂了就没有结论可判，kw_rate 记 None（不知道），
                    # 不是 0——0 会把它当成"关键词一个都没中"拖低覆盖率的平均值，
                    # 而这条本来就是 env 失败、不该参与模型能力统计。
                    "kw_rate": None, "kw_na": False,
                    "steps_ok": False, "blocked": [], "misquoted": [],
                    "trace_ok": False, "fc_fail": True,
                    "cite_stats": {}, "round_ok": False, "round_detail": "",
                    "turns": len(traj.get("turns") or []),
                    "final_head": "",
                }

    def record(traj, state, r):
        with lock:
            got[traj["id"]] = r
            # 逐次追加落盘：report_eval.json 只保留最后一次，会覆盖历史，
            # 诊断"同一输入为什么这次对那次错"必须有每一次的结论正文。
            try:
                if state is None:
                    return
                rec = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "commit": meta["commit"],
                    "model": meta["model"],
                    "id": traj["id"],
                    "semantic_ok": r["semantic_ok"],
                    "kw_rate": r["kw_rate"],
                    "req_miss": r.get("req_miss", []),
                    "steps": r["steps"],
                    "tools": r["tools"],
                    "reasons": r["reasons"],
                    "failure": r.get("failure") or [],
                    "cite_stats": r.get("cite_stats") or {},
                    "final": state.get("final", "") or "",
                    # 证据全文一起落盘：否则改了校验规则想复盘"当时为什么被判未溯源"，
                    # 手里只有结论、没有它引的那些数据，只能整份评测重跑一遍（25 条约 50 万 token）。
                    # 带上 evidence 之后，任何一次历史评测都能离线重算判定。
                    "evidence": [
                        e.get("result") if isinstance(e.get("result"), str)
                        else json.dumps(e.get("result"), ensure_ascii=False, default=str)
                        for e in (state.get("evidence") or [])
                    ],
                }
                with (ROOT / "eval" / "history.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                # 通过的用例顺手存一份参考解。以后改判据时，这些"已知的好答案"
                # 会被重新判一遍——如果它们红了，问题在判据不在模型。
                # 依据：Anthropic《Demystifying evals》Step 2 的 reference solution。
                if r.get("semantic_ok"):
                    from eval import reference as _ref
                    _ref.save_reference(traj, state, meta)
            except Exception:
                pass  # 落盘失败不能中断评测

    t0 = time.time()
    if parallel:
        print(f"[并发批] {len(parallel)} 条，workers={workers}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, t) for t in parallel]
            for f in as_completed(futs):
                record(*f.result())
    if serial:
        print(f"[串行批] {len(serial)} 条（建单类共享外部状态 / 多轮类共用 checkpointer）")
        for t in serial:
            record(*one(t))
    elapsed = time.time() - t0

    results = [got[t["id"]] for t in targets if t["id"] in got]

    for r in results:
        flag = "OK" if r["semantic_ok"] else "FAIL"
        print(f"[{r['id']}] {flag} 步数{r['steps']} 工具{len(r['tools'])}")
        if not r["semantic_ok"]:
            print(f"     原因: {'; '.join(r['reasons'])}")
            codes = [f["code"] for f in (r.get("failure") or [])]
            if codes:
                print(f"     归因: {', '.join(codes)}")

    n = len(results)
    ok = sum(1 for r in results if r["semantic_ok"])
    fc = sum(1 for r in results if r["fc_fail"])
    blocked_n = sum(1 for r in results if r["blocked"])
    # 有 misquoted、但那条是"整理答案的模型调用失败"的，不算引用写错：
    # 之前这条兜底输出的是纯文本，于是被判 json_bad，报告上就成了
    # "结论未按 claims 契约输出"——把中转站超时报成模型违规，归因错人。
    # 现在单独一类，两行分开计数，谁出问题一眼看得出。
    svc_n = sum(1 for r in results
                if (r.get("cite_stats") or {}).get("svc_fail"))
    misquote_n = sum(1 for r in results
                     if r.get("misquoted") and not (r.get("cite_stats") or {}).get("svc_fail"))
    trace_ok_n = sum(1 for r in results if r.get("trace_ok"))
    # blocked 为空不等于溯源干净：没标角标、但"全证据池里恰好有这个数"也算放行，
    # 这类值占了大多数。真正该看的是明确出处率 = cited_ok / qty。
    cite_hit = sum((r.get("cite_stats") or {}).get("cited_ok", 0) for r in results)
    cite_qty = sum((r.get("cite_stats") or {}).get("qty", 0) for r in results)
    cite_rate = (cite_hit / cite_qty) if cite_qty else 0.0
    # 引文型 claim（knowledge）：模型抄的那句原文，回原文核对的结果。
    # 单独一行、不和"明确出处率"混：那一行的分母是"带数字的事实"，
    # 而 knowledge 的结论多数压根没有数字——混在一起会让两条路径都读不准。
    quote_ok = sum((r.get("cite_stats") or {}).get("quote_ok", 0) for r in results)
    quote_bad = sum((r.get("cite_stats") or {}).get("quote_bad", 0) for r in results)
    # 证据超长被压缩、字段没送进去：这类引用无法核验，但不是模型写错。
    # 单独计数，否则它会躲在"引用写错"里，把证据问题报成模型问题。
    cite_cut = sum((r.get("cite_stats") or {}).get("cite_cut", 0) for r in results)
    # 区间 claim（`6%~11%`）的两种结局必须分开报：
    #   range_ok   两端都在 ref 指向的那段原文里，引用成立
    #   range_bad  撑不住——ref 指着数值字段，或某个端点不在原文里
    # 只报成立数会让人以为"区间没问题"；只报失败数又看不出模型到底用没用区间。
    range_ok = sum((r.get("cite_stats") or {}).get("range_ok", 0) for r in results)
    range_bad = sum((r.get("cite_stats") or {}).get("range_bad", 0) for r in results)
    # 结论 JSON 里值内裸引号被自动修好才解析成功的条数。
    # 这个必须看得见：修复是静默的，不单独计数就等于"模型越不守契约越没人知道"。
    json_fixed = sum(1 for r in results
                     if (r.get("cite_stats") or {}).get("json_fixed"))
    steps_bad = sum(1 for r in results if not r["steps_ok"])
    # 被预算掐断的条数。这个值非零就说明"12 步上限"在替模型做判断，
    # 那几条的结论是半成品。以前它们混在步数里、和正常收工长得一样，
    # 既看不见次数，也拿不到归因。
    budget_n = sum(1 for r in results
                   if r.get("stop_reason") == STOP_BUDGET_EXHAUSTED)
    # 正常收工（模型自己判断够了）的条数，和上面那个数成对出现：
    # 只看一个数分不出"没发生过预算耗尽"和"根本没统计"。
    enough_n = sum(1 for r in results if r.get("stop_reason") == "enough")
    # 被 check 拦下、回环一次才通过的条数。0 有两种可能：
    # 模型本来就没写错，或者 check 根本没生效——后者看 misquote_n 和
    # "区间引用"那一行是否都为 0 也判断不出来，只能靠 at_check 的实测样本区分。
    check_n = sum(1 for r in results if r.get("check_rounds"))
    # 结论里写了数字、claims 却给空数组的条数。
    # 它不是"引用写错"，是"压根没有可核验的对象"——qty 会是 0，两行汇总都看不见它。
    # 必须单独一行，否则这种通过方式是隐形的。
    bare_n = sum(1 for r in results
                 if (r.get("cite_stats") or {}).get("bare_conclusion"))

    print("\n" + "=" * 60)
    print(f"语义正确率    {ok}/{n} = {ok / n:.1%}   （只看结论对不对，不溯源）")
    print(f"明确出处率    {cite_hit}/{cite_qty} = {cite_rate:.1%}   （数字带 ref 且指到字段的比例）")
    print(f"引文核对      {quote_ok} 成 / {quote_bad} 败   （模型抄的原文能否在片段里逐字找到）")
    print(f"结论空 claims {bare_n}/{n}   （结论写数字但不给 claims，等于把内容挪到检查外）")
    print(f"工具命中率    {sum(r['tool_rate'] for r in results) / n:.3f}（平均）")
    print(f"维度覆盖率    {sum(r['gb_rate'] for r in results) / n:.3f}（平均）")
    # 关键词覆盖 ≠ 整条通过。语义正确率（上面第一行）才是"整条判通过"的比例；
    # 这一行只回答"设了关键词的那些用例，结论里的关键点中了几成"。
    # 分母只算设了关键词的条数——原来按 n 平均，等于把 4 条没设关键词的
    # 默认 1.0 也算进去，凭空抬高了这个数。
    kw_n = sum(1 for r in results if r.get("kw_rate") is not None)
    kw_sum = sum(r["kw_rate"] for r in results if r.get("kw_rate") is not None)
    kw_na_n = n - kw_n
    kw_part = f"{kw_sum / kw_n:.3f}（{kw_n} 条有设关键词" if kw_n else "不适用（0 条设关键词"
    kw_part += f"，{kw_na_n} 条未设、不计入）" if kw_na_n else "）"
    print(f"关键词覆盖    {kw_part}   （只管设了关键词的结论，不等于答得好）")
    print(f"步数越界      {steps_bad}/{n}   （按用例期望区间判）")
    print(f"预算耗尽      {budget_n}/{n}   （还想查但额度已尽，这几条结论是半成品）")
    print(f"判断够了收工  {enough_n}/{n}   （模型自己判断证据够了，正常结束）")
    print(f"引用自查回环  {check_n}/{n}   （被 check 拦下、把原因回喂后重出才通过的条数）")
    intent_bad = sum(1 for r in results if r.get("intent_ok") is False)
    print(f"意图判错      {intent_bad}/{n}")
    # 规则层的好坏必须成三行看，只报"命中几条"等于没报：
    #   命中  = 规则接管了多少条
    #   救人  = 模型本来会判错（model_intent ≠ 期望），规则把它纠对了
    #   误伤  = 规则自己也判错（最终 intent ≠ 期望）——代价最贵，因为它绕过了模型的判据
    # model_intent 为空（跑挂/没给）的条数不进"救人"分母，否则会被当成"模型判错"。
    rule_hit = [r for r in results if (r.get("route_by") or "").startswith("rule:")]
    rule_saved = [r for r in rule_hit
                  if r.get("expect_intent") and r.get("model_intent")
                  and r["model_intent"] != r["expect_intent"]
                  and r.get("intent") == r["expect_intent"]]
    rule_harm = [r for r in rule_hit
                 if r.get("expect_intent") and r.get("intent") != r["expect_intent"]]
    print(f"规则命中      {len(rule_hit)}/{n}   （规则接管的条数；命中多不等于规则有效，看下面两行）")
    print(f"规则救人      {len(rule_saved)} 条   （模型本来会判错、规则纠正了）")
    print(f"规则误伤      {len(rule_harm)} 条   （规则自己判错，比模型判错更贵）")
    # 单看"命中 8"最容易的误读是"规则有效"。把三个数压成一个净收益，
    # 让"8 条命中但 0 救 0 伤"这个事实在报表上自己浮出来——
    # 这不是说规则没用，只是这批用例没覆盖到模型会判错的场景，测不出收益。
    net = len(rule_saved) - len(rule_harm)
    if net == 0:
        print(f"规则净收益    {net:+d}   （救人-误伤；为 0 = 这批没测出规则的增量价值，"
              f"不表示规则无用）")
    else:
        print(f"规则净收益    {net:+d}   （救人-误伤）")
    # 低置信 != 判错。这一行统计的是"有多少条结论是带不确定交付的"——
    # 用户会在结论里看到"按「X」理解"，或者干脆被改成追问。
    # 把它和 intent_bad 分开，才能看出模型是在诚实报不确定，还是在判错。
    conf_low = sum(1 for r in results
                   if (r.get("intent_conf") if r.get("intent_conf") is not None else 1.0) < CONF_OK)
    print(f"意图低置信    {conf_low}/{n}   （带'按X理解'或改成追问，不是判错）")
    print(f"fc 失败       {fc}/{n}")
    # 该反问却没反问：信息不全时直接作答，用户会以为它真知道了。
    # 和"意图判错"分开——意图判对了但懒得追问，是另一种毛病。
    ask_bad = sum(1 for r in results if r.get("ask_ok") is False)
    # 幂等"没验证"和"没通过"必须分开报：前者是环境没起，
    # 后者是业务真的没去重。混成一个数，修错地方都不知道。
    dedup_grey = sum(1 for r in results if r.get("deduped_env"))
    print(f"该反问未反问  {ask_bad}/{n}   （信息不全时该追问，直接作答等于假装知道）")
    print(f"幂等未验证    {dedup_grey}/{n}   （建单工具连不上，这项判不了，标灰放行）")
    print(f"有未溯源数字  {blocked_n}/{n}   （ref 写 null，标灰放行，不计失败）")
    # "不计失败"不等于"答得好"。这行把"判通过但有未溯源数字"的条数单列：
    # 结论可读却不可核，它出现在语义正确率的分子里，不出现在这行就会隐形。
    grey_pass = sum(1 for r in results
                    if r.get("semantic_ok") and (r.get("cite_stats") or {}).get("unsourced"))
    print(f"标灰通过      {grey_pass}/{n}   （判通过但有未溯源数字：结论可读，但核不了）")
    # 派生值：ref 为 null 但给了算式，判据重算过。
    # 和上一行成对出现——给了算式却算错的是 failure，没算式的是标灰。
    # 混在一起就分不出"模型会算"和"模型在编"，而这两者的修法完全不同。
    d_ok = sum((r.get("cite_stats") or {}).get("derived_ok", 0) for r in results)
    d_bad = sum((r.get("cite_stats") or {}).get("derived_bad", 0) for r in results)
    print(f"派生值重算    {d_ok} 成 / {d_bad} 败   （ref 为空但给了算式：重算对得上才算数）")
    print(f"引用写错      {misquote_n}/{n}   （伪引用，计入失败）")
    # "引用歧义"是"引用写错"的子集：同一工具按多种参数各调过、ref 又没写参数指纹，
    # 判据不知道它指哪一次。仍计入失败，单独一行是为了让读报告的人先看到"该补指纹"，
    # 而不是"模型算错了"——两种修法完全不同（改 prompt vs 改引用格式）。
    amb_n = sum((r.get("cite_stats") or {}).get("ref_ambiguous", 0) for r in results)
    print(f"引用歧义      {amb_n} 条引用   （同一工具多次调用且未写指纹，判据无法确定指哪次；已计入引用写错）")
    # 多值 claim（items）的两种结局。只报失败数会让人以为"没人用这个字段"，
    # 只报成立数又会让人以为"多值引用没问题"——两行必须一起看。
    multi_ok = sum((r.get("cite_stats") or {}).get("multi_ok", 0) for r in results)
    multi_bad = sum((r.get("cite_stats") or {}).get("multi_bad", 0) for r in results)
    print(f"多值引用      {multi_ok} 成 / {multi_bad} 败   （items 数组：每条「标识+数值」都要在返回里配上对）")
    print(f"服务失败      {svc_n}/{n}   （整理答案时模型调用失败，结论是半成品，不计入引用写错）")
    print(f"证据超长截断  {cite_cut} 条引用   （返回体装不下，标灰放行，不算引用写错）")
    print(f"引号自动修复  {json_fixed}/{n}   （值内裸引号破坏 JSON，修格式后重跑全部校验）")
    print(f"区间引用      {range_ok} 成 / {range_bad} 败   （ref 指文本字段且两端都在原文 = 成；"
          f"ref 指数值字段 = 败）")
    # 多轮单独一行：这一行 0/0 说明本次没跑多轮用例，不是"多轮没问题"。
    # 和上面那些行一样，分母为 0 时必须显式看见，不能被"没跑"伪装成"全绿"。
    multi = [r for r in results if r.get("turns")]
    if multi:
        m_ok = sum(1 for r in multi if r.get("round_ok"))
        print(f"多轮引用成立  {m_ok}/{len(multi)}   （最后一轮所有 ref 在本轮可见证据里成立）")
    else:
        print("多轮引用成立  未运行   （本次没有多轮用例）")
    # 失败归因分布。不统计的话 failure 只躺在逐条明细里，看总览时仍然
    # 得一条条点开才知道"这五个红里哪几个是答偏、哪几个是引用写错"。
    # 分母按 code 累计而不是按条数：一条用例可能同时踩两个坑。
    kinds = {}
    for r in results:
        for f in (r.get("failure") or []):
            kinds[f["code"]] = kinds.get(f["code"], 0) + 1
    if kinds:
        top = sorted(kinds.items(), key=lambda kv: -kv[1])
        print(f"失败归因      {' / '.join(f'{c}×{v}' for c, v in top)}")
    else:
        print("失败归因      无   （这一轮没有失败；旧报告没有 failure 字段也会显示这行）")
    print(f"总耗时        {elapsed:.0f} s（平均 {elapsed / n:.1f} s/条）")

    # 报告从"一个裸数组"变成"带身份的结构体"：meta 说清这份报告在什么条件下产生，
    # summary 放聚合数，results 保留逐条明细。旧报告仍是裸数组，不要倒退着改它们。
    commit = meta["commit"] or "nogit"
    stamp = time.strftime("%Y%m%d_%H%M")
    summary = {
        "n": n,
        "semantic_ok": ok,
        "semantic_rate": round(ok / n, 4) if n else 0.0,
        "tool_rate": round(sum(r["tool_rate"] for r in results) / n, 4) if n else 0.0,
        "gb_rate": round(sum(r["gb_rate"] for r in results) / n, 4) if n else 0.0,
        # 关键词覆盖只在"设了关键词"的用例上求平均，分母是 kw_n 不是 n。
        # 把没设关键词的按 1.0 算进去是假满分：它会同时抬高指标和掩盖"判据没验过"。
        "kw_rate": (round(kw_sum / kw_n, 4) if kw_n else None),
        "kw_n": kw_n,
        "kw_na": kw_na_n,
        "steps_bad": steps_bad,
        "budget_exhausted": budget_n,
        "enough": enough_n,
        "check_rounds_n": check_n,
        "intent_bad": intent_bad,
        # 规则层的三个数：命中 / 救人 / 误伤。只有命中数时看不出好坏，
        # 而"误伤"是这套方案最贵的失败模式——规则绕过了模型判据，错了没人兜。
        "rule_hit": len(rule_hit),
        "rule_saved": len(rule_saved),
        "rule_harm": len(rule_harm),
        # 净收益：命中数本身不说明规则有用，救人-误伤才是。
        "rule_net": len(rule_saved) - len(rule_harm),
        "ask_bad": ask_bad,
        "deduped_grey": dedup_grey,
        "intent_conf_low_n": conf_low,
        "fc_fail": fc,
        "blocked_n": blocked_n,
        "trace_ok_n": trace_ok_n,
        "trace_rate": round(trace_ok_n / n, 4) if n else 0.0,
        "cite_hit": cite_hit,
        "cite_qty": cite_qty,
        "failure_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "cite_rate": round(cite_rate, 4),
        "cite_cut": cite_cut,
        "json_fixed": json_fixed,
        "misquote_n": misquote_n,
        "range_ok": range_ok,
        "range_bad": range_bad,
        "elapsed_s": round(elapsed, 1),
    }
    report = {"meta": meta, "summary": summary, "results": results}
    text = json.dumps(report, ensure_ascii=False, indent=1)

    # 带 commit 的归档件：这一份只对应这一个代码版本，以后能和别的报告比
    out_full = ROOT / "eval" / f"report_{stamp}_{commit}.json"
    out_full.write_text(text, encoding="utf-8")
    # 同名副本保持老引用可用（README 与几篇 docs 都指向 report_eval.json）
    (ROOT / "eval" / "report_eval.json").write_text(text, encoding="utf-8")

    print(f"\n明细已写入 {out_full.name}（report_eval.json 同步为最新副本）")
    print("这一份要登记进 eval/报告状态.md，否则下次仍判断不了它能不能和别的报告比")


if __name__ == "__main__":
    # 必须把返回值传出去：--only 写错编号要能用退出码判断（脚本里 `if ($LASTEXITCODE)` 就靠它）。
    # 原来裸调 main()，退出码永远是 0，"没匹配到用例"在 CI 里和"跑完了"长得一样。
    raise SystemExit(main())
