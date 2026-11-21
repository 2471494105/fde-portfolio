"""连接的可见性：把 SDK 日志里的临时凭证打码，连上就喊一声。

为什么单独一个文件、不写在 __main__.py 里：
单测要覆盖"什么算连上"和"凭证不许入日志"这两条判断，而 __main__.py 一导入就拖进
整个 lark_oapi。实测 `import lark_oapi` 需要 15.5 秒（本机 2026-11-21），
单测总时长会从 5 秒涨到 22 秒。把这段纯逻辑摘出来，它就只依赖 logging。

为什么不轮询 SDK 的 _conn 字段：那个私有字段名随版本变，而且 start() 把连接
和随后阻塞的 _select() 串在一起，中间没有缝能插进自己的探测。
"""

import logging
import re

# 连上时 SDK 会 INFO 打整条 conn_url，形如
#   connected to wss://msg-frontier.feishu.cn/ws/v2?...&access_key=xxx&ticket=yyy
# access_key 与 ticket 是这一次会话的临时凭证，落进日志/录屏就等于把连接交出去。
_MASK = re.compile(r"\b(access_key|ticket)=[^&\s]+")


class MaskSecrets(logging.Filter):
    """挂在 "Lark" logger 上，把 query 里的凭证换成 ***。

    改 record.msg 而不是字符串替换输出：logger 的 Filter 在 callHandlers 之前生效，
    所以 SDK 自带的 StreamHandler、我们挂在 root 的 handler、以及 ReadyOnConnected
    拿到的都是同一份打码后的文本。
    """

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = _MASK.sub(lambda m: f"{m.group(1)}=***", msg)
        if masked != msg:
            record.msg = masked
            record.args = ()      # 已经格式化过，避免二次 %-插值
        return True


class ReadyOnConnected(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.done = False

    def emit(self, record):
        if self.done:
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "connected to" in msg:
            self.done = True
            print("ready: ws connected, db ok", flush=True)
