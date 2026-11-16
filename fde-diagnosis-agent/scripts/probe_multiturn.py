"""探针：同一个 session 连问同一个问题两次，看第二轮会不会复用上一轮的证据。

要回答的三个问题：
  1. 第二轮 steps 是不是 0（有没有真的重新查）
  2. 第二轮的 evidence 是本轮新查的，还是把上一轮的也带上了
  3. 结论里的数字到底是新数据还是旧数据

这三件事决定了"多轮会话"是特性还是缺陷。
"""
import json
import time

import requests

BASE = "http://127.0.0.1:8020"
MSG = "CX-105 最近一个月误判越来越多，但标定记录都是正常的，什么原因"
# 必须每次新 session：写死的 id 会带着上一轮的 evidence 进来，第一次就变成 turn=3
SID = "probe-mt-" + time.strftime("%H%M%S")


def run_once(msg, sid):
    got = {"turn": None, "steps": None, "ev": [], "final": "", "node_ms": []}
    with requests.post(f"{BASE}/api/diagnose/stream",
                       json={"msg": msg, "session_id": sid},
                       stream=True, timeout=900) as r:
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            f = json.loads(line[6:])
            t = f.get("type")
            if t == "node_end":
                got["node_ms"].append((f.get("node"), f.get("ms")))
            elif t == "done":
                got["turn"] = f.get("turn")
                got["steps"] = f.get("steps")
                got["ev"] = f.get("evidence") or []
                got["final"] = f.get("final") or ""
            elif t == "error":
                print("  错误帧:", f.get("message"))
    return got


for i in (1, 2):
    r = run_once(MSG, SID)
    cur = sum(1 for e in r["ev"] if e.get("current"))
    his = len(r["ev"]) - cur
    print(f"\n===== 第 {i} 次 =====")
    print(f"turn={r['turn']}  steps={r['steps']}  evidence 累计={len(r['ev'])} 条"
          f"（本轮 {cur} / 历史 {his}）  结论 {len(r['final'])} 字")
    print("evidence 工具序列:", [e["tool"] for e in r["ev"]])
    if i == 1:
        assert cur == len(r["ev"]) and his == 0, "第一轮不应有历史证据"
        print("PASS 第一轮：全部标为本轮")
    else:
        assert cur == r["steps"], f"本轮标记数 {cur} 应等于 steps {r['steps']}"
        assert his > 0, "第二轮应保留上一轮证据"
        print(f"PASS 第二轮：{cur} 条标本轮、{his} 条标历史，界面可分层展示")
