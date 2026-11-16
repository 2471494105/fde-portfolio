"""轨迹落盘。

为什么要自己写而不是用 LangSmith：离线可跑、不依赖外部服务、面试时能直接翻文件看。
每条 run 一个 JSONL 文件，一行一个事件，按时间顺序追加。

记录粒度刻意细到"单次 LLM 调用"和"单次工具调用"——
只有到这一步，才能回答"这次诊断慢在哪一步""token 花在取证上还是花在复述上"。
"""

import contextvars
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from app.meta import stamp as _version_stamp

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"

_current = contextvars.ContextVar("current_tracer", default=None)


def current():
    return _current.get()


def emit(event, **payload):
    """当前 run 有 tracer 就写一行；没有就静默跳过（评测里不想落盘时用）。"""
    t = _current.get()
    if t is not None:
        t.write(event, **payload)


class Tracer:
    def __init__(self, user_msg: str, model: str = "", note: str = "",
                 provider: str = "", base_url: str = ""):
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
        self.path = RUNS_DIR / f"{self.run_id}.jsonl"
        self.t0 = time.perf_counter()
        self.tokens = 0
        self._fh = None
        # 观测设施不能把主流程拖下水：目录不可写、磁盘满、文件被占用，
        # 都只降级成"这次没有轨迹"，整轮 run 必须照常跑完。
        # 之前这里没有保护，runs/ 一变只读就会让每一轮诊断直接抛异常。
        self.broken = False
        try:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("w", encoding="utf-8")
        except Exception:
            self.broken = True

        # provider / base_url 必须单独记：同一个模型名可能挂在多个中转站上，
        # 只记 model 事后分不清这批数据是谁跑的、按谁家的价算。
        # commit / code_hash 在这里自动采集，不由调用方传参：
        # 每加一个新入口就记得补这两个参数是不现实的，漏一次就断一条链。
        try:
            ver = _version_stamp()
        except Exception:
            ver = {"commit": "", "code_hash": ""}
        self.write("run_start", user_msg=user_msg, model=model, note=note,
                   provider=provider, base_url=base_url,
                   commit=ver.get("commit", ""), code_hash=ver.get("code_hash", ""))

    def write(self, event, **payload):
        if self._fh is None:
            return
        rec = {
            "t": round((time.perf_counter() - self.t0) * 1000),  # 相对起始的毫秒，便于看节奏
            "event": event,
            **payload,
        }
        try:
            self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        except Exception:
            # 一条写失败就不再尝试：否则后面每个 emit 都要再抛一次、再捕获一次，
            # 反而把一次磁盘问题放大成整条链路的开销。
            self.broken = True
            self._fh = None

    def add_tokens(self, n: int):
        self.tokens += int(n or 0)

    def close(self, **summary):
        summary.setdefault("tokens_total", self.tokens)
        summary.setdefault("elapsed_ms", round((time.perf_counter() - self.t0) * 1000))
        self.write("run_end", **summary)
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    # 上下文管理：with Tracer(...) as t: run(...)
    def __enter__(self):
        _current.set(self)
        return self

    def __exit__(self, *exc):
        _current.set(None)
        self.close(error=str(exc[1]) if exc[0] else None)
        return False

    # 流式生成器不能用 with（yield 会把作用域拉长），改用显式进出。
    # 之前漏了这一步，导致节点内的 trace.emit / add_tokens 全部静默丢弃：
    # 流式跑出来的 jsonl 只有 node_start / node_end，token 恒为 0。
    def activate(self):
        _current.set(self)

    def deactivate(self):
        _current.set(None)


def list_runs():
    """按时间倒序列出已有轨迹文件。"""
    if not RUNS_DIR.exists():
        return []
    return sorted(RUNS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def load(path):
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
