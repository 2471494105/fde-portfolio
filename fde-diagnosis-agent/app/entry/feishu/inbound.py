"""事件 → InboundMessage。飞书的字段名到此为止，业务层永远只见这七个。

为什么不让业务层直接吃 event：只要 nodes.py 里出现过 chat_id，换入口就得改图，
而图是"判定"，不该为入口改动。这条线就靠这个结构守着——`raw` 只给日志用，
里面刻意只挑三个无关痛痒的字段，省得有人顺手拿去判业务。
"""

import hashlib
import json
from dataclasses import dataclass, field

CHANNEL = "feishu"
THREAD_PREFIX = "fs-"


@dataclass(frozen=True)
class InboundMessage:
    channel: str              # 恒为 "feishu"
    user_key: str             # open_id：入口内唯一的用户标识
    room_key: str             # chat_id：会话容器
    text: str                 # 去掉 @机器人 占位符之后的正文
    event_id: str             # message_id：入口判重用
    thread_hint: str | None   # 话题群的 thread；不用话题群时是 None
    raw: dict = field(default_factory=dict, repr=False)   # 只给日志，业务层不许读


def thread_id(room_key: str, user_key: str) -> str:
    """会话键 = fs- + sha256(chat_id|open_id)[:16]。

    两个人在同一个群里问同一台设备，必须各自一条会话，否则一个人追问"那换个工位呢"
    会接到另一个人上一轮的上下文里。所以 open_id 也要进哈希。

    加 fs- 前缀是为了和 Web 的裸 uuid 分开：Web 的 thread_id 存在浏览器 localStorage，
    用户手里可能还留着旧值，改 Web 那一侧的格式等于让所有旧会话失效（DOC-36 §13.3）。
    """
    digest = hashlib.sha256(f"{room_key}|{user_key}".encode("utf-8")).hexdigest()
    return THREAD_PREFIX + digest[:16]


def parse(event) -> tuple["InboundMessage | None", str]:
    """(消息, 跳过原因)。原因非空时调用方只记一行日志，不报错、也不回执失败。

    只接 text：post（富文本）要按 tag 重新拼句子，file/image 根本没有文本，
    这两种在演示脚本（DOC-36 §八）里不会出现，真接要各自设计交互，
    硬塞进来只会得到一堆"空文本"事件。
    """
    ev = getattr(event, "event", None)
    msg = getattr(ev, "message", None)
    if msg is None:
        return None, "no-message"

    mtype = (getattr(msg, "message_type", "") or "").strip()
    if mtype != "text":
        return None, f"unsupported-type:{mtype or '-'}"

    sender = getattr(ev, "sender", None)
    if (getattr(sender, "sender_type", "") or "").strip() == "app":
        # 机器人发的消息也会走这个事件（比如另一个机器人），它不提问
        return None, "from-app"

    sender_id = getattr(sender, "sender_id", None)
    user_key = (getattr(sender_id, "open_id", "") or "").strip() if sender_id else ""
    room_key = (getattr(msg, "chat_id", "") or "").strip()
    event_id = (getattr(msg, "message_id", "") or "").strip()
    if not (user_key and room_key and event_id):
        return None, "missing-identity"

    text = _text_of(msg)
    if not text:
        return None, "empty-text"

    return (
        InboundMessage(
            channel=CHANNEL,
            user_key=user_key,
            room_key=room_key,
            text=text,
            event_id=event_id,
            thread_hint=(getattr(msg, "thread_id", "") or "").strip() or None,
            raw={
                "chat_type": getattr(msg, "chat_type", ""),
                "message_type": mtype,
                "create_time": getattr(msg, "create_time", None),
            },
        ),
        "",
    )


def _text_of(msg) -> str:
    """text 消息的 content 是 JSON 字符串；群聊正文里 @机器人 会变成 @_user_1 占位。

    占位符不是他要问的内容。不抠掉的话，understand 节点收到的第一句是
    "@_user_1 CX-101 又误报了"，设备名和整句意图都会被这一小段噪声带偏。
    """
    try:
        payload = json.loads(getattr(msg, "content", "") or "{}")
    except (TypeError, ValueError):
        return ""
    text = payload.get("text") or ""
    for m in (getattr(msg, "mentions", None) or []):
        key = (getattr(m, "key", "") or "").strip()
        if key:
            text = text.replace(key, "")
    return text.strip()
