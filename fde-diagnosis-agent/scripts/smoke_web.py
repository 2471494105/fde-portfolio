"""Web 接口冒烟：首页 / 诊断 / 历史列表 / 轨迹回放 四个接口。

用法：先起服务，再跑本脚本
    python -m uvicorn app.web.server:app --host 127.0.0.1 --port 8020
    python scripts\\smoke_web.py
"""

import sys

import httpx

BASE = "http://127.0.0.1:8020"


def main():
    ok = True

    r = httpx.get(f"{BASE}/", timeout=10)
    print(f"[1] 首页       HTTP {r.status_code}，{len(r.text)} 字节")
    ok &= r.status_code == 200 and "<title>" in r.text

    msg = "CX-105 最近一个月误判越来越多，但标定记录都是正常的，什么原因"
    r = httpx.post(f"{BASE}/api/diagnose", json={"msg": msg}, timeout=300)
    d = r.json()
    print(f"[2] 诊断       HTTP {r.status_code}  步数 {d['steps']}  "
          f"工具 {len(d['tools'])} 次  未溯源 {len(d['blocked'])}  trace {d['trace_id']}")
    print(f"               结论开头: {d['final'][:110].replace(chr(10), ' ')}")
    ok &= r.status_code == 200 and d["steps"] > 0 and bool(d["trace_id"])

    r = httpx.get(f"{BASE}/api/runs", timeout=10)
    runs = r.json()["runs"]
    print(f"[3] 历史列表   HTTP {r.status_code}，共 {len(runs)} 条")
    ok &= r.status_code == 200 and len(runs) >= 1

    r = httpx.get(f"{BASE}/api/runs/{d['trace_id']}", timeout=10)
    ev = r.json()["events"]
    kinds = sorted({e["event"] for e in ev})
    print(f"[4] 轨迹回放   HTTP {r.status_code}，{len(ev)} 个事件 {kinds}")
    ok &= r.status_code == 200 and "plan" in kinds and "run_end" in kinds

    # 空输入应被拒绝
    r = httpx.post(f"{BASE}/api/diagnose", json={"msg": "   "}, timeout=10)
    print(f"[5] 空输入     HTTP {r.status_code}（应为 400）")
    ok &= r.status_code == 400

    print("\n结果:", "全部通过" if ok else "有失败项")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
