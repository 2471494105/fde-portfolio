"""测本地模型的真实上下文上限。

为什么要测：Ollama 超长 prompt 是静默截断，不报错。如果不量出实际
prompt_eval_count，会以为模型看到了全部证据，其实后半截根本没进模型。
"""

import time

import httpx

BASE = "http://localhost:11434"
MODEL = "qwen2.5:7b"
TEXT = "这是一段用于测量上下文的填充文本。" * 600   # 约 10200 字


def probe(num_ctx=None):
    opts = {"num_predict": 2}
    if num_ctx:
        opts["num_ctx"] = num_ctx
    body = {"model": MODEL, "messages": [{"role": "user", "content": TEXT}],
            "stream": False, "options": opts}
    t = time.time()
    try:
        d = httpx.post(f"{BASE}/api/chat", json=body, timeout=900).json()
        return d.get("prompt_eval_count"), time.time() - t
    except Exception as e:
        return f"失败 {e}", time.time() - t


if __name__ == "__main__":
    print(f"输入字符数：{len(TEXT)}")
    for nc in (None, 4096, 8192):
        n, dt = probe(nc)
        label = "默认" if nc is None else f"num_ctx={nc}"
        print(f"{label:<14} 实际 prompt_eval_count={n}  耗时={dt:.1f}s")
