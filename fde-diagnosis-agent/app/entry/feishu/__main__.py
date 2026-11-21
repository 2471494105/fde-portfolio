"""python -m app.entry.feishu —— 飞书入口独立进程。

为什么是独立进程、不塞进 uvicorn：
lark 的 ws.Client.start() 会在它模块级那个 asyncio loop 上 run_until_complete 之后
一直阻塞，塞进 FastAPI 就是和 Web 共用一条线程，两边一起卡。两个进程只共用
FDE_PG_DSN 与 app.entry.stream 里的帧，反而干净（DOC-36 §13.1）。

退出码：
  0  正常结束（Ctrl+C）
  1  长连接建立失败（SDK 抛上来的）
  2  缺凭据（缺哪个打在 stderr）
  3  数据库不通
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

from app.entry.feishu import inbound, render
from app.entry.feishu.client import FeishuClient, SendFailed
from app.entry.feishu.config import MissingCreds, load
from app.entry.feishu.dedupe import Dedupe
from app.entry.feishu.ready import ReadyOnConnected

EXIT_OK = 0
EXIT_WS = 1
EXIT_MISSING_CREDS = 2
EXIT_DB = 3

log = logging.getLogger("fde.entry.feishu")

# 图一轮 7–23 秒，回调线程里绝不能等它；4 个槽够演示（同时两个人问也不会互相排队）
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fde-fs")



def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        creds = load()
    except MissingCreds as e:
        # 不降级成"只记日志"：看着起来了却收不到消息，是现场最难查的一类故障
        print(f"missing env: {', '.join(e.keys)}", file=sys.stderr, flush=True)
        return EXIT_MISSING_CREDS

    dedupe = Dedupe()
    try:
        dedupe.setup()
        dedupe.prune()
    except Exception as e:
        print(f"db failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return EXIT_DB
    print("db: ok", flush=True)

    client = FeishuClient(creds.app_id, creds.app_secret)
    handler = _build_handler(creds, client, dedupe)

    # 让 SDK 的连接日志可见，否则 ready 那一行永远不会触发
    lark_logger = logging.getLogger("Lark")
    lark_logger.setLevel(logging.INFO)
    lark_logger.addHandler(ReadyOnConnected())

    ws = lark.ws.Client(creds.app_id, creds.app_secret,
                        log_level=lark.LogLevel.INFO, event_handler=handler)
    print("ws: connecting...", flush=True)
    try:
        ws.start()
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return EXIT_OK
    except Exception as e:
        # 凭据错、应用没发布、长连接没开通，SDK 都从这一处抛上来。
        # 只打一行：traceback 有二十几行，真正的错误码（如 1000040346 app_id is invalid）
        # 会被埋掉，现场第一步要看的就是那个码。
        print(f"ws failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        print("hint: 应用是否已发布版本并通过审批？长连接需要 im:message 相关权限"
              "（见 docs/20261121_02_双入口工程表.md 的 W3）", file=sys.stderr, flush=True)
        return EXIT_WS
    print("ws: closed", flush=True)
    return EXIT_OK


def _build_handler(creds, client: FeishuClient, dedupe: Dedupe) -> EventDispatcherHandler:
    def on_message(data: P2ImMessageReceiveV1) -> None:
        # 这个回调跑在 SDK 的 asyncio loop 线程里。慢动作会把心跳和其他事件一起卡住，
        # 所以这里只做三件快事：归一、判重、丢线程池。
        msg, why = inbound.parse(data)
        if msg is None:
            log.info("skip: %s", why)
            return
        # 判重必须留在回调里同步做：一旦丢进线程池，两条重复事件会同时通过检查，
        # 这套机制就白设了。本地 PG 一次 1–2 ms，占不满循环。
        if dedupe.seen(msg.channel, msg.event_id):
            log.info("dup: %s", msg.event_id)
            return
        log.info("in: room=%s user=%s event=%s chars=%d",
                 msg.room_key, msg.user_key, msg.event_id, len(msg.text))
        _pool.submit(_handle, msg, client)

    return (EventDispatcherHandler
            .builder(creds.encrypt_key, creds.verification_token)
            .register_p2_im_message_receive_v1(on_message)
            .build())


def _handle(msg: inbound.InboundMessage, client: FeishuClient) -> None:
    """W2 的边界：只走通"收 → 判重 → 发卡"这条管道，不跑图。

    W4/W5 会在这里接 app.entry.stream.iter_events，把帧一路 PATCH 进同一张卡。
    现在显式不跑，是为了让"管道通了"和"能诊断"在演示前能被分开验证——
    不然第一次真机联调时，收不到、判重错、图和卡片任何一处出问题都混在同一个症状里。
    """
    tid = inbound.thread_id(msg.room_key, msg.user_key)
    try:
        card = render.build([], title="故障诊断",
                            footer=f"骨架版本：只回执不诊断 · 会话 {tid}")
        client.send_card(msg.room_key, card)
        log.info("sent: room=%s thread=%s", msg.room_key, tid)
    except SendFailed as e:
        log.warning("send_card failed: %s", e)
        try:
            client.send_text(msg.room_key, f"收到，但这张卡片没发出去（{e.code}）")
        except SendFailed as e2:
            log.error("send_text failed too: %s", e2)


if __name__ == "__main__":
    sys.exit(main())
