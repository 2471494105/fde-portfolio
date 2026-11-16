"""测 OpenAI 兼容端点能不能透传 num_ctx。

主链路走的是 /v1/chat/completions（为了和图里的 messages/tool_calls 结构一致），
但 num_ctx 是 Ollama 私有参数，不在 OpenAI 协议里。这里验证 extra_body 是否生效。
"""

import time

from openai import OpenAI

TEXT = "这是一段用于测量上下文的填充文本。" * 600
client = OpenAI(api_key="ollama", base_url="http://localhost:11434/v1", timeout=900)


def run(label, extra_body=None):
    kw = dict(model="qwen2.5:7b", messages=[{"role": "user", "content": TEXT}],
              max_tokens=2, temperature=0)
    if extra_body:
        kw["extra_body"] = extra_body
    t = time.time()
    r = client.chat.completions.create(**kw)
    print(f"{label:<26} prompt_tokens={r.usage.prompt_tokens}  "
          f"耗时={time.time() - t:.1f}s")


if __name__ == "__main__":
    run("不带 extra_body")
    run("extra_body num_ctx=8192", {"options": {"num_ctx": 8192}})
