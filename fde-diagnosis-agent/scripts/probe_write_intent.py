# -*- coding: utf-8 -*-
"""写意图闸的真实路径验证：闸必须挡在 HTTP 之前，不是挡在假实现之前。

单测把整个 create_work_order 换成了假函数，那只证明"act 没调那个假函数"。
这里保留真实的 create_work_order（含参数校验、设备存在性查询），
只把 httpx.post 换成记录器——于是"有没有到达 QMS"成为可直接观察的事实。

三个场景：
  A 诊断问题（无写意图）  → 拦下，到达 HTTP 的次数必须是 0
  B 显式要求建单         → 放行，到达 HTTP 的次数必须是 1
  C 参数非法             → 不进 HTTP，problem_codes 必须能按 code 聚合

不对 mock QMS 产生任何真实写入：HTTP 被替换成记录器，只读的库查询照常走。

跑法（项目根目录）：
    venv\\Scripts\\python.exe scripts\\probe_write_intent.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph import nodes
from app.tools import workorder


class _Rec:
    def __init__(self):
        self.posts = []

    def __len__(self):
        return len(self.posts)


class _Resp:
    status_code = 200
    text = ('{"code":0,"message":"created",'
            '"data":{"order_id":"WO-PROBE","status":"open","assignee_group":"设备科"}}')

    def json(self):
        return json.loads(self.text)


def _send(question):
    """让 act 处理一次 create_work_order，返回 (工具结果, 到达 HTTP 的次数)。"""
    rec = _Rec()

    def fake_post(*a, **kw):
        rec.posts.append(kw.get("json"))
        return _Resp()

    msgs = []
    if question is not None:
        msgs.append({"role": "user", "content": question})
    msgs.append({
        "role": "assistant",
        "tool_calls": [{
            "id": "c1",
            "function": {
                "name": "create_work_order",
                "arguments": json.dumps({
                    "device_id": "CX-101",
                    "root_cause": "探针：光源超额定寿命",
                    "root_cause_code": "light_degradation",
                    "severity": "high",
                    "actions": ["更换光源"],
                    "evidence_refs": ["query_light_usage#cumulative_hours"],
                }, ensure_ascii=False),
            },
        }],
    })
    state = {"messages": msgs, "device_ids": ["CX-101"], "device_id": "CX-101", "turn": 1}

    orig = workorder.httpx.post
    workorder.httpx.post = fake_post
    try:
        out = nodes.act(state)
    finally:
        workorder.httpx.post = orig

    res = nodes._load_obj({"result": out["messages"][0]["content"]})
    return res, len(rec)


def main():
    ok = True

    # A：用户只问原因，模型却想建单 → 必须拦在建单之前
    res, sent = _send("CX-101 的夜班误判率为什么一直偏高，帮我查下")
    a_ok = res.get("blocked_by") == "write_intent" and sent == 0
    print(f"[A] 无写意图  blocked_by={res.get('blocked_by')!r}  到达HTTP={sent}"
          f"  → {'通过' if a_ok else '失败'}")
    ok &= a_ok

    # B：用户明说建单 → 放行到 HTTP 层（用的是记录器，不真写）
    res, sent = _send("帮 CX-101 建个单报修，光源超寿命了")
    b_ok = "error" not in res and sent == 1
    print(f"[B] 有写意图  error={res.get('error')!r}  到达HTTP={sent}"
          f"  → {'通过' if b_ok else '失败'}")
    ok &= b_ok

    # C：参数非法 → 不进 HTTP，驳回理由要能按 code 聚合
    res3 = workorder.create_work_order(
        device_id="CX-101", root_cause="探针", root_cause_code="light_degradation",
        severity="high", actions=[], evidence_refs=[])
    codes = {(c["code"], c["field"]) for c in res3.get("problem_codes") or []}
    c_ok = (bool(res3.get("error")) and ("empty_list", "actions") in codes
            and len(res3.get("problems") or []) == len(res3.get("problem_codes") or []))
    print(f"[C] 参数非法  codes={sorted(codes)}  → {'通过' if c_ok else '失败'}")
    ok &= c_ok

    print("\n判定：", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
