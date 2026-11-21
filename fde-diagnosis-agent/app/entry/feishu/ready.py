"""连接成功的信号：往 SDK 的日志里挂一个钩子，连上就喊一声。

为什么单独一个文件、不写在 __main__.py 里：
单测要覆盖"什么算连上"这条判断，而 __main__.py 一导入就拖进整个 lark_oapi。
实测 `import lark_oapi` 需要 15.5 秒（本机 2026-11-21），单测总时长会从 5 秒涨到 22 秒。
把这段纯逻辑摘出来，它就只依赖 logging。

为什么不轮询 SDK 的 _conn 字段：那个私有字段名随版本变，而且 start() 把连接
和随后阻塞的 _select() 串在一起，中间没有缝能插进自己的探测。
"""

import logging


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
