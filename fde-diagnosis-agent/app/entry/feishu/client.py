"""发送侧：只做发卡、改卡、兜底发文本。

不判断根因、不读台账、不碰图的节点。适配器一旦会"想"，业务口径就会在入口分叉成两份，
演示时最丢人的莫过于"网页上是这个结论、飞书上是另一个"。
"""

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

log = logging.getLogger("fde.entry.feishu")


class SendFailed(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
        super().__init__(f"{code}: {msg}")


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, log_level=lark.LogLevel.INFO):
        self._client = (lark.Client.builder()
                        .app_id(app_id)
                        .app_secret(app_secret)
                        .log_level(log_level)
                        .build())

    def send_card(self, chat_id: str, card: dict, *, uuid: str = "") -> str:
        """发一张卡，返回 message_id（后面 PATCH 全靠它）。

        uuid 是发送侧的幂等键：同一个 uuid 一小时内至多成功一条。
        重试发"处理中"卡时带上它，免得用户看见两张一样的卡在同时转。
        """
        body = (CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(_content(card)))
        if uuid:
            body.uuid(uuid)
        req = (CreateMessageRequest.builder()
               .receive_id_type("chat_id")
               .request_body(body.build())
               .build())
        resp = self._client.im.v1.message.create(req)
        if resp.code != 0:
            raise SendFailed(resp.code, resp.msg)
        return resp.data.message_id

    def patch_card(self, message_id: str, card: dict) -> None:
        """原地改卡。

        飞书的流式卡片大约 10 分钟会被服务端强制收尾，之后 PATCH 静默丢弃
        （DOC-37 记过，非官方文档）。本项目一轮 7–23 秒碰不到这条线，
        但调用方别把它当"总会生效"——失败要能落到 send_text。
        """
        req = (PatchMessageRequest.builder()
               .message_id(message_id)
               .request_body(PatchMessageRequestBody.builder()
                             .content(_content(card)).build())
               .build())
        resp = self._client.im.v1.message.patch(req)
        if resp.code != 0:
            raise SendFailed(resp.code, resp.msg)

    def send_text(self, chat_id: str, text: str) -> str:
        """卡片链路失败时的兜底。宁可难看，也不能让人以为消息没送到。"""
        body = (CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build())
        req = (CreateMessageRequest.builder()
               .receive_id_type("chat_id")
               .request_body(body)
               .build())
        resp = self._client.im.v1.message.create(req)
        if resp.code != 0:
            raise SendFailed(resp.code, resp.msg)
        return resp.data.message_id


def _content(card: dict) -> str:
    # 紧凑序列化：卡片有 30 KB 上限，空白字符也算钱
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))
