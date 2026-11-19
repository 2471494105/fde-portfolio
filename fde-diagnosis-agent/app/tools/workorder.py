"""工具 7：建单（幂等），走 mock QMS 的 HTTP 接口。

为什么不直接写库：真实企业系统里建单要走 QMS/MES 的接口，
而这类接口最常见的坑是——业务失败时 HTTP 仍然返回 200，错误码藏在响应体的 code 字段。
只看 resp.status_code 会把"被拒绝的建单"当成成功，Agent 就会告诉用户"已建单"而实际没建。

所以这里做了三道检查：HTTP 状态码、响应体是否 JSON、body.code 是否为 0。
"""

import os

import httpx

from app.tools.db import connect
from app.tools.hints import HINT

QMS_URL = os.environ.get("FDE_QMS_URL", "http://127.0.0.1:8010")

# 枚举值必须和 schemas.py 里给模型的 enum 一模一样。
# 这不是重复劳动：schema 里的 enum 只是"告诉模型"，运行时不强制，
# 模型填 "calibration drift"（带空格）也不会报错，而它恰恰是幂等键的一部分——
# 键一变，同一台设备同一个根因就会被再建一张单。
ROOT_CAUSE_CODES = {
    "light_degradation", "lens_contamination", "calibration_drift",
    "batch_drift", "threshold_misconfig", "other",
}
SEVERITIES = {"high", "medium", "low"}


def _check_order_args(device_id, root_cause, root_cause_code, severity, actions, evidence_refs):
    """建单前置校验。返回 (归一后的字段, 人读问题列表, 机读问题码)。

    为什么必须在客户端做：mock QMS 不校验业务字段，真实 QMS 也大多只校验格式。
    "证据为空也建单"在系统里是能成功的，只是事后没人能复核这张单为什么存在。

    为什么同时给两份：`problems` 是自由文案，人读得懂但按不住（"root_cause_code 不在枚举内"
    改一个字就匹配不上了）；`problem_codes` 是 {"code","field","note"}，能被程序和看板聚合。
    结论层（verify 的 cite_fail）早就是带 code 的结构，动作层却只有字符串——
    同一个失败信号两种承载，这两份一起给就是为了让动作层对齐结论层。
    """
    did = str(device_id or "").strip().upper()
    cause = str(root_cause or "").strip()
    code = str(root_cause_code or "").strip()
    sev = str(severity or "").strip().lower()
    acts = [str(a).strip() for a in (actions or []) if str(a).strip()]
    refs = [str(r).strip() for r in (evidence_refs or []) if str(r).strip()]

    problems = []
    codes = []

    def _add(code_name, field, note):
        problems.append(note)
        codes.append({"code": code_name, "field": field, "note": note})

    if not did:
        _add("empty_field", "device_id", "device_id 为空")
    if not cause:
        _add("empty_field", "root_cause", "root_cause 为空")
    if code not in ROOT_CAUSE_CODES:
        _add("not_in_enum", "root_cause_code", f"root_cause_code 不在枚举内：{code!r}")
    if sev not in SEVERITIES:
        _add("not_in_enum", "severity", f"severity 不在枚举内：{sev!r}")
    if not acts:
        _add("empty_list", "actions", "actions 为空，没有处置动作不该建单")
    if not refs:
        _add("empty_list", "evidence_refs", "evidence_refs 为空，没有证据的工单事后无法复核")

    return {"device_id": did, "root_cause": cause, "root_cause_code": code,
            "severity": sev, "actions": acts, "evidence_refs": refs}, problems, codes


def _device_exists(did):
    """建单前确认设备在台账里。返回 (是否存在, 错误信息)。

    为什么要单独查一次：mock QMS（以及多数真实 QMS）只校验字段格式，
    拿一个不存在的设备号也能建单成功，工单就挂在一台查无此机的设备上。
    三态必须分清：DB 不可用 != 设备不存在，前者该降级重试，后者该追问编号。
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select 1 from devices where device_id = %s;", (did,))
            return cur.fetchone() is not None, None
    except Exception as exc:
        return None, f"设备存在性校验失败（数据库不可用）：{exc}"


def create_work_order(device_id: str, root_cause: str, root_cause_code: str, severity: str,
                      actions: list, evidence_refs: list) -> dict:
    fields, problems, problem_codes = _check_order_args(device_id, root_cause, root_cause_code,
                                                        severity, actions, evidence_refs)
    if problems:
        return {
            "error": "建单参数校验未通过，工单未创建",
            "problems": problems,
            "problem_codes": problem_codes,
            "allowed_root_cause_code": sorted(ROOT_CAUSE_CODES),
            "allowed_severity": sorted(SEVERITIES),
            "hint": (
                "补齐或改正后重试。不要用近义写法代替枚举值——"
                "枚举值同时是幂等键的一部分，写错会让同一张单被建两次。"
            ),
        }

    exists, err = _device_exists(fields["device_id"])
    if err:
        return {"error": err, "state": "system_unavailable"}
    if not exists:
        return {
            "error": f"设备 {fields['device_id']} 不在设备台账里，不能建单",
            "state": "device_not_found",
            "hint": "先确认设备编号，或让用户核对铭牌后再建单。",
        }

    # 用归一后的字段：大小写、首尾空格不影响幂等键，
    # 否则 "cx-101" 和 "CX-101" 会各自生成一个键，去重形同虚设。
    payload = dict(fields)

    try:
        resp = httpx.post(f"{QMS_URL}/api/work-order", json=payload, timeout=10)
    except httpx.HTTPError as exc:
        return {"error": f"mock QMS 不可达（{QMS_URL}）：{exc}"}

    if resp.status_code != 200:
        return {
            "error": f"QMS HTTP 异常：{resp.status_code}",
            "http_status": resp.status_code,
            "body_head": resp.text[:120],
        }

    try:
        body = resp.json()
    except ValueError:
        return {
            "error": "QMS 返回非 JSON，无法判断建单是否成功",
            "http_status": resp.status_code,
            "body_head": resp.text[:120],
        }

    # 最关键的一步：HTTP 200 也要看业务码
    if body.get("code") != 0:
        return {
            "error": "QMS 业务失败，工单未创建",
            "http_status": resp.status_code,
            "biz_code": body.get("code"),
            "biz_message": body.get("message"),
        }

    data = body.get("data") or {}
    if not data.get("order_id"):
        return {"error": "QMS 返回成功但缺少 order_id", "raw": body}

    return {
        "order_id": data["order_id"],
        "status": data.get("status"),
        "deduped": data.get("deduped", False),
        "assignee_group": data.get("assignee_group"),
        "message": (
            "该设备这类故障已有一张未关闭的工单，本次未新建，返回原来那张"
            if data.get("deduped")
            else f"已建单，分派至{data.get('assignee_group')}"
        ),
    }


def query_work_order(device_id: str = "", status: str = "", limit: int = 10) -> dict:
    """工具 8：查历史工单（只读）。

    为什么必须有这个工具：之前只有 create_work_order（写），没有读。
    用户问"上次那个工单处理了吗"时模型无路可走——要么编一个 WO 号，要么说查不到。
    这是"系统结构上没答案"而不是"模型不聪明"的典型，补一个只读工具成本极低。

    三态口径（沿用项目一的教训，不能把三种情况混成一个"没查到"）：
      A. 设备不存在      -> 明确说设备编号不在台账里，不要让用户以为"只是没工单"
      B. 设备存在无工单  -> 明确说台账里有这台设备，但没有任何工单记录
      C. 查到工单        -> 返回列表，按建单时间倒序

    只读工具，不写库、不改状态。
    """
    did = (device_id or "").strip().upper()
    st = (status or "").strip()

    # 范围闸：设备与状态都不给 = 把全厂整张工单表拖出来。必须拦，不是洁癖：
    #   ① 一台设备的诊断能读到全厂工单 —— 范围被悄悄放大；
    #   ② 10 条无关工单会挤掉真正要看的那条，上下文预算被无关证据吃掉；
    #   ③ 以前它默默放行，模型看到一堆工单会当成"这台设备的"，结论直接错。
    # 只拒"两个都空"这一种：『全厂有哪些待处理的』是合法需求（device 空 + status 给定）。
    if not did and not st:
        return {
            "device_id": None,
            "count": 0,
            "orders": [],
            "state": "bad_args",
            "error": "查工单至少要给一个范围：device_id 或 status，不能两个都空",
            "hint": (
                "要么传 device_id（如 CX-101）查单台设备，"
                "要么传 status（如 待处理）查全厂该状态的单。"
                "两个都不传等于拖全厂整表，既没有诊断价值，也会挤掉真正该看的证据。"
            ),
        }

    # limit 收敛，避免模型传个 1000 把整表拖出来。
    # 全厂查询再压一档：没有设备维度时，每条都是"可能无关"的样本。
    cap = 50 if did else 20
    try:
        limit = max(1, min(int(limit or 10), cap))
    except (TypeError, ValueError):
        limit = 10

    # 返回恒带 scope：模型看不见"这是全厂还是单台"，就会把全厂结果当成单台的。
    # 边界必须随结果一起回来，不能靠它的记忆。
    scope = f"device:{did}" if did else f"全厂(status={st})"

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                if did:
                    cur.execute("select device_id from devices where device_id = %s;", (did,))
                    if not cur.fetchone():
                        return {
                            "device_id": did,
                            "scope": scope,
                            "count": 0,
                            "orders": [],
                            "state": "device_not_found",
                            "note": f"设备 {did} 不在设备台账里，无法查其工单。请确认编号是否正确。",
                            # hint 是按 Anthropic《Writing effective tools for agents》的做法：
                            # 错误响应要写"具体且可行动的改进"，而不是只给一个不透明的事实。
                            # 只说"设备不存在"，模型只能猜下一步——实测它去查了
                            # query_device_profile，白烧一次调用（probe_unknown_multiturn）。
                            "hint": HINT["device_not_found"],
                        }

                sql = ("select order_id, device_id, root_cause, severity, status, "
                       "assignee_group, created_at from work_orders")
                cond, args = [], []
                if did:
                    cond.append("device_id = %s")
                    args.append(did)
                if st:
                    cond.append("status = %s")
                    args.append(st)
                if cond:
                    sql += " where " + " and ".join(cond)
                sql += " order by created_at desc limit %s"
                args.append(limit)
                cur.execute(sql, args)
                rows = cur.fetchall()
    except Exception as exc:
        return {"error": f"查工单失败：{exc}"}

    orders = []
    for r in rows:
        orders.append({
            "order_id": r["order_id"],
            "device_id": r["device_id"],
            "root_cause": r["root_cause"],
            "severity": r["severity"],
            "status": r["status"],
            "assignee_group": r["assignee_group"],
            "created_at": str(r["created_at"])[:19],
        })

    if not orders:
        scope_text = f"设备 {did}" if did else "全厂"
        extra = f"、状态为「{st}」" if st else ""
        return {
            "device_id": did or None,
            "scope": scope,
            "count": 0,
            "orders": [],
            "state": "no_order",
            "note": f"{scope_text}{extra}没有任何工单记录。这是查询结果，不是建单记录缺失。",
            "hint": HINT["no_order"],
        }

    return {
        "device_id": did or None,
        "scope": scope,
        "count": len(orders),
        "orders": orders,
        "state": "found",
        "note": "按建单时间倒序，最多返回 limit 条。",
    }
