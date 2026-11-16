"""验证 Web 端多轮：同一个 session_id 连续三次请求，追问要能接上。

前端把 session_id 放 localStorage，后端原样当 thread_id。
这个脚本模拟三次点击，走的是真实的 HTTP 入口，不是直接调图。
"""

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8020"
# 每次跑用新 session，否则会接着上一次的 turn 往下走，测的就不是"从零开始"
RUN_TAG = str(int(time.time()))
SID = f"web-smoke-{RUN_TAG}"


def ask(msg, sid=SID):
    r = httpx.post(f"{BASE}/api/diagnose", json={"msg": msg, "session_id": sid}, timeout=300)
    r.raise_for_status()
    return r.json()


def show(tag, d):
    print(f"\n--- {tag} ---")
    print(f"turn={d.get('turn')} intent={d.get('intent')} steps={d.get('steps')} "
          f"slots={d.get('slots')} thread={d.get('thread_id')}")
    print(f"结论：{(d.get('final') or '')[:200].strip()}")


def main():
    print("=" * 70)
    print("Web 多轮：模糊 → 补设备 → 看数据")
    d1 = ask("帮我看看最近怎么样")
    show("第1轮", d1)
    d2 = ask("CX-101")
    show("第2轮", d2)
    d3 = ask("看数据")
    show("第3轮", d3)

    problems = []
    if d2.get("turn") != 2:
        problems.append(f"第2轮 turn={d2.get('turn')}，state 没接上")
    if "设备编号" in (d3.get("final") or ""):
        problems.append("第3轮又问设备编号，槽位没记住")
    if d3.get("steps", 0) == 0:
        problems.append("第3轮没查数据")
    if "误判率" not in (d3.get("final") or ""):
        problems.append("第3轮没出数据表")

    print("\n" + "=" * 70)
    # 换一个 session 应该是全新一轮
    d4 = ask("CX-102 批次误判多", sid=f"web-smoke-{RUN_TAG}-b")
    print(f"新会话 turn={d4.get('turn')}（应为 1）")
    if d4.get("turn") != 1:
        problems.append("换 session 后没有重新开始")

    # 非法 session_id 应被拒
    r = httpx.post(f"{BASE}/api/diagnose", json={"msg": "x", "session_id": "../../etc"},
                   timeout=30)
    print(f"非法 session_id → HTTP {r.status_code}（应 400）")
    if r.status_code != 400:
        problems.append("非法 session_id 未被拦截")

    print("\n判定：", "通过" if not problems else "失败：" + "；".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
