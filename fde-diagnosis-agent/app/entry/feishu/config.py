"""飞书凭据：只从环境变量读，缺哪个就报哪个。

为什么不做"没凭据也能起来、只是不发消息"：
演示现场最难查的故障就是这类——进程看着正常、日志也干净，一条消息却进不来。
宁可退出码 2，让缺的东西在第一行就露出来。
"""

import os
from dataclasses import dataclass

# 只为触发 .env 加载（app.graph.llm 在模块级调 _load_env，内部用 setdefault，重复调用无害）
from app.graph.llm import _load_env

REQUIRED = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
OPTIONAL = ("FEISHU_VERIFICATION_TOKEN", "FEISHU_ENCRYPT_KEY")


class MissingCreds(Exception):
    """必填项缺失。__main__ 把它翻译成退出码 2 和一行"缺什么"。"""

    def __init__(self, keys):
        self.keys = list(keys)
        super().__init__(", ".join(self.keys))


@dataclass(frozen=True)
class Creds:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str


def missing() -> list[str]:
    _load_env()
    return [k for k in REQUIRED if not (os.environ.get(k) or "").strip()]


def load() -> Creds:
    keys = missing()
    if keys:
        raise MissingCreds(keys)
    return Creds(
        app_id=os.environ["FEISHU_APP_ID"].strip(),
        app_secret=os.environ["FEISHU_APP_SECRET"].strip(),
        # 长连接模式下 SDK 不做签名校验，这两项留空是合法的。
        # 还是读它们：将来真要走 webhook，只改这一处装配，不用回头再动配置层。
        verification_token=(os.environ.get("FEISHU_VERIFICATION_TOKEN") or "").strip(),
        encrypt_key=(os.environ.get("FEISHU_ENCRYPT_KEY") or "").strip(),
    )
