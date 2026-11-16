"""流式接口验证：按"用户能不能看见进展"这个标准测，不是按"接口通没通"测。

五条断言：
  1. 首帧在 1.5s 内到达（用户点完按钮立刻有反应）
  2. 第一个 node_start 在 10s 内到达（最慢的 plan 之前，界面已经有内容）
  3. node_start / node_end 成对，没有孤立事件
  4. done 帧存在且 final 非空
  5. done.total_ms 与客户端实测总时长差 < 5s（服务端没有假装跑完）
"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8020"
DEFAULT_MSG = "CX-104 从 11月8号开始误判突然变多，之前一直好好的"


def run(msg):
    t0 = time.perf_counter()
    marks = []
    frames = []
    with requests.post(f"{BASE}/api/diagnose/stream", json={"msg": msg},
                       stream=True, timeout=300) as r:
        if r.status_code != 200:
            print("HTTP", r.status_code, r.text[:400])
            return None
        print(f"HTTP {r.status_code}  content-type={r.headers.get('content-type')}")
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            at = time.perf_counter() - t0
            try:
                obj = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            frames.append((at, obj))
            typ = obj.get("type")
            if typ == "open":
                print(f"{at:7.2f}s  [open] thread={obj.get('thread_id')}")
            elif typ == "node_start":
                print(f"{at:7.2f}s  -> {obj['label']} 开始")
            elif typ == "node_end":
                print(f"{at:7.2f}s  <- {obj['label']} 完成 {obj['ms']}ms  {obj.get('detail','')}")
            elif typ == "done":
                print(f"{at:7.2f}s  [done] steps={obj.get('steps')} tools={len(obj.get('tools') or [])} "
                      f"total={obj.get('total_ms')}ms trace={obj.get('trace_id')}")
            elif typ == "error":
                print(f"{at:7.2f}s  [error] {obj.get('message')}")
    return time.perf_counter() - t0, frames


def check(frames):
    ok = True
    def say(name, cond, extra=""):
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")

    first = frames[0][0] if frames else 9e9
    say("1 首帧 < 1.5s", first < 1.5, f"实测 {first:.2f}s")

    starts = [f for f in frames if f[1].get("type") == "node_start"]
    first_start = starts[0][0] if starts else 9e9
    say("2 首个 node_start < 10s", first_start < 10, f"实测 {first_start:.2f}s  "
        f"{starts[0][1]['label'] if starts else ''}")

    n_start = len(starts)
    n_end = len([f for f in frames if f[1].get("type") == "node_end"])
    say("3 start/end 成对", n_start == n_end and n_start > 0, f"start={n_start} end={n_end}")

    done = [f for f in frames if f[1].get("type") == "done"]
    has_final = bool(done) and bool(done[0][1].get("final"))
    say("4 done 帧且有结论", has_final, f"final 长度 {len(done[0][1]['final']) if done else 0}")

    return ok, (done[0][1] if done else None)


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MSG
    print(f"输入：{msg}\n")
    res = run(msg)
    if not res:
        return
    total, frames = res
    print(f"\n客户端实测总时长 {total:.2f}s")
    print("\n--- 断言 ---")
    ok, done = check(frames)
    if done:
        gap = abs(total * 1000 - done.get("total_ms", 0))
        print(f"  {'PASS' if gap < 5000 else 'FAIL'}  5 服务端/客户端总时长一致  差 {gap/1000:.2f}s")
        if gap >= 5000:
            ok = False
    print("\n结果：", "全部通过" if ok else "有失败项")


main()
