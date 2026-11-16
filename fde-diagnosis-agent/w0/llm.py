"""统一模型调用层：ollama / DeepSeek 共用一套中间消息格式。

中间格式：
  {"role": "user" | "system", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
  {"role": "tool", "name": str, "tool_call_id": str, "content": str}

这样 agent_loop 只认中间格式，换后端不用改编排逻辑。
"""

import json
import os
from pathlib import Path


def _load_env():
    p = Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


class OllamaClient:
    def __init__(self, model="qwen2.5:7b"):
        from ollama import chat
        self._chat = chat
        self.model = model
        self.name = f"ollama:{model}"

    def complete(self, messages, tools=None):
        omsgs = []
        for m in messages:
            role = m["role"]
            if role == "tool":
                omsgs.append({"role": "tool", "tool_name": m["name"], "content": m["content"]})
            elif role == "assistant" and m.get("tool_calls"):
                omsgs.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [
                        {"function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in m["tool_calls"]
                    ],
                })
            else:
                omsgs.append({"role": role, "content": m.get("content") or ""})

        resp = self._chat(model=self.model, messages=omsgs, tools=tools,
                          options={"num_ctx": 4096, "temperature": 0})
        msg = resp["message"]
        calls = []
        for c in (msg.get("tool_calls") or []):
            args = c["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": "", "name": c["function"]["name"], "arguments": args})
        return {"content": msg.get("content") or "", "tool_calls": calls}


class DeepSeekClient:
    def __init__(self, model="deepseek-chat", api_key=None):
        _load_env()
        from openai import OpenAI
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY，请写入 .env 或设置环境变量")
        self.client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
        self.model = model
        self.name = f"deepseek:{model}"

    def complete(self, messages, tools=None):
        omsgs = []
        for m in messages:
            role = m["role"]
            if role == "tool":
                omsgs.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                              "content": m["content"]})
            elif role == "assistant" and m.get("tool_calls"):
                omsgs.append({
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {
                            "id": c["id"] or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                            },
                        }
                        for i, c in enumerate(m["tool_calls"])
                    ],
                })
            else:
                omsgs.append({"role": role, "content": m.get("content") or ""})

        kwargs = {"model": self.model, "messages": omsgs, "temperature": 0}
        if tools:
            kwargs["tools"] = tools
        resp = self.client.chat.completions.create(**kwargs)
        m = resp.choices[0].message
        calls = []
        for c in (m.tool_calls or []):
            try:
                args = json.loads(c.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append({"id": c.id, "name": c.function.name, "arguments": args})
        return {"content": m.content or "", "tool_calls": calls}


def get_client(backend="ollama", model=""):
    if backend == "deepseek":
        return DeepSeekClient(model=model or "deepseek-chat")
    return OllamaClient(model=model or "qwen2.5:7b")
