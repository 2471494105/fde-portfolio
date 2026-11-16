"""轨迹回放：把一次 run 的 JSONL 打印成时间线。

用法：
    python scripts/replay.py --list                 列出所有轨迹
    python scripts/replay.py                        回放最新一条
    python scripts/replay.py --id 20261116_0930_ab12  回放指定
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import trace  # noqa: E402


def fmt_t(ms):
    if ms < 1000:
        return f"{ms:>6}ms"
    return f"{ms / 1000:>6.1f}s"


def show(path):
    events = trace.load(path)
    print(f"=== {path.name} ===")

    for e in events:
        t = fmt_t(e.get("t", 0))
        ev = e["event"]

        if ev == "run_start":
            print(f"[{t}] 开始    model={e.get('model')}")
            print(f"          输入: {e.get('user_msg')}")
        elif ev == "intake":
            if e.get("device_id"):
                print(f"[{t}] 识别    设备号 {e['device_id']}")
            else:
                print(f"[{t}] 识别    未抽到设备号 → 追问")
        elif ev == "ask_user":
            print(f"[{t}] 追问    缺少设备号，程序直接生成追问")
        elif ev == "plan":
            calls = "、".join(e.get("calls") or []) or "（无工具调用，准备收尾）"
            print(f"[{t}] 推理    token {e.get('prompt_tokens')}+{e.get('completion_tokens')}  "
                  f"{(e.get('ms', 0) / 1000):.1f}s → {calls}")
        elif ev == "tool":
            mark = "ok  " if e.get("ok") else "ERR "
            print(f"[{t}] 工具    {mark}{e.get('name')}  {(e.get('ms', 0))}ms  "
                  f"{str(e.get('result_head', ''))[:110]}")
            if not e.get("ok"):
                print(f"          错误: {e.get('error')}")
        elif ev == "verify":
            blocked = e.get("blocked") or []
            tail = f"未溯源 {blocked}" if blocked else "数字全部可溯源"
            print(f"[{t}] 校验    {tail}（结论含 {e.get('pct_count')} 个百分比，{e.get('final_len')} 字）")
        elif ev == "result":
            print(f"[{t}] 结果    步数 {e.get('steps')} / 工具 {len(e.get('tools') or [])} 次 "
                  f"/ 未溯源 {len(e.get('blocked') or [])} 个")
            print(f"          结论开头: {str(e.get('final_head', ''))[:150]}")
        elif ev == "run_end":
            print(f"[{t}] 结束    总耗时 {(e.get('elapsed_ms', 0) / 1000):.1f}s，"
                  f"token 合计 {e.get('tokens_total')}")
            if e.get("error") and e["error"] != "None":
                print(f"          异常: {e['error']}")


def main():
    args = sys.argv[1:]
    runs = trace.list_runs()

    if "--list" in args:
        if not runs:
            print("还没有轨迹文件。先跑一次 scripts\\run_case.py")
            return
        print(f"共 {len(runs)} 条轨迹（按时间倒序）：\n")
        for p in runs[:20]:
            ev = trace.load(p)
            head = next((x for x in ev if x["event"] == "run_start"), {})
            end = next((x for x in ev if x["event"] == "run_end"), {})
            msg = str(head.get("user_msg", ""))[:42]
            print(f"  {p.stem}   {(end.get('elapsed_ms', 0) / 1000):>5.1f}s   {msg}")
        return

    target = None
    for i, a in enumerate(args):
        if a == "--id" and i + 1 < len(args):
            target = trace.RUNS_DIR / f"{args[i + 1]}.jsonl"

    if target is None:
        if not runs:
            print("还没有轨迹文件。先跑一次 scripts\\run_case.py")
            return
        target = runs[0]

    if not target.exists():
        print(f"找不到轨迹文件: {target}")
        return
    show(target)


if __name__ == "__main__":
    main()
