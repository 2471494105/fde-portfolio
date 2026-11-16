"""LLM 适配层：三种 provider，靠环境变量切换，不靠改代码。

- deepseek  ：官方端点（默认）
- ollama    ：本地 Ollama，零成本兜底，断网可演示
- openai    ：任意 OpenAI 兼容端点（中转站/自建网关），必须配 FDE_BASE_URL

配置（写在项目根 .env，_load_env 会读进 os.environ）：
    FDE_PROVIDER=openai
    FDE_BASE_URL=https://minekuai.cn/v1
    FDE_MODEL=deepseek-v4.1-flash-1017
    FDE_API_KEY=sk-xxx          # 没写则回落到 DEEPSEEK_API_KEY

为什么默认官方 API 而不是本地（2026-11-16 同 case 实测）：
- 语义正确率 95.8% vs 25%，本地在取证维度上系统性不全（缺 station / 缺 day / 不建单）
- 本地并不省 token：同一条 H3，DeepSeek 21853（7 步走完），本地 9739（2 步停手）。
  本地"省下"的 55% 是没干完的活。
- 耗时 4.1s vs 35.6s（6GB 显存装不下 7b，必走 CPU）

换本地必须的前置：Ollama 要能吃下 3000+ token 的 prompt（单条 plan 已 2146）。
Ollama 默认上下文 2050，超长 prompt 静默截断且不报错——
需设用户级 OLLAMA_CONTEXT_LENGTH=8192 后重启 Ollama，实测可吃到 6629。

任何新 provider 接入前，先跑 scripts/probe_provider.py 证明它真的返回 tool_calls：
中转站常把 tool_calls 降级成普通文本，那样图里所有分支都失效。
"""

import os
from pathlib import Path

from openai import OpenAI

_ROOT = Path(__file__).resolve().parents[2]


def _load_env():
    for path in (_ROOT / ".env", _ROOT / "w0" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

PROVIDER = os.environ.get("FDE_PROVIDER", "deepseek").strip().lower()

# 各 provider 的默认模型；FDE_MODEL 覆盖一切
_DEFAULTS = {
    "ollama": "qwen2.5:7b",
    "deepseek": "deepseek-chat",
    "openai": "deepseek-v4.1-flash-1017",
}
MODEL = os.environ.get("FDE_MODEL") or _DEFAULTS.get(PROVIDER, "deepseek-chat")

BASE_URL = os.environ.get("FDE_BASE_URL", "").strip()
if not BASE_URL and PROVIDER == "deepseek":
    BASE_URL = "https://api.deepseek.com"

# 本地上下文的硬上限。Ollama 超长 prompt 是静默截断（不报错），
# 所以这个数必须和 nodes.py 的 MAX_CTX_TURNS 一起算，不能各写各的。
LOCAL_NUM_CTX = int(os.environ.get("FDE_NUM_CTX", "4096"))

# 单次调用超时。必须集中成一个变量：nodes.py 里曾各自硬编码 20s/30s，
# 换了中转站之后实测单次调用会出现 54.7s（同参同 prompt，18 倍波动），
# 那些硬编码会绕开 client 的 timeout，表现为"意图偶尔判错/知识分支答不出"，
# 而且不报错、只在结果上体现，极难排查。
CALL_TIMEOUT = int(os.environ.get("FDE_TIMEOUT", "180" if PROVIDER == "ollama" else "120"))

_client = None


def get_client() -> OpenAI:
    """OpenAI 兼容客户端。

    本地也走 OpenAI 兼容端点（Ollama 的 /v1），这样图里的 messages / tool_calls
    结构和 API 完全一致，切 provider 不用改调用方。
    """
    global _client
    if _client is None:
        if PROVIDER == "ollama":
            base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
            if not base.startswith("http"):
                base = "http://" + base
            # 6GB 显存装不下 7b，会走 CPU，单次可能十几秒；超时给宽一点
            _client = OpenAI(api_key="ollama", base_url=f"{base}/v1",
                             timeout=CALL_TIMEOUT, max_retries=1)
        else:
            api_key = os.environ.get("FDE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError("缺少 key：设置 FDE_API_KEY（项目根 .env 或 w0/.env）")
            if not BASE_URL:
                raise RuntimeError("FDE_PROVIDER=openai 时必须设置 FDE_BASE_URL，例如 https://xxx/v1")
            # 中转站质量不稳定，失败时重试一次比直接抛错有用
            _client = OpenAI(api_key=api_key, base_url=BASE_URL,
                             timeout=CALL_TIMEOUT, max_retries=2)
    return _client
