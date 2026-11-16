"""用 CDP 驱动 headless Edge，逐秒抓「实时执行」面板的真实状态。

为什么要这么麻烦：
  msedge --dump-dom 的 --virtual-time-budget 只快进虚拟时钟，不等真实网络，
  流式响应还没回来页面就被 dump 走了，什么都看不到。
  CDP 能真实等待，并且可以按秒采样 DOM —— 这才是"用户看到的界面在动态推进"的证据。

产出：
  终端打印时间线（每秒一行：正在做什么 / 已几秒 / 已完成几步）
  过程截图三张：约 3s、约 10s、结束
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9223
MAGIC = "cdp-stream-probe"
OUT = Path(tempfile.gettempdir())

PROBE_JS = r"""
(() => {
  const n = document.getElementById('liveNow');
  const rows = [...document.querySelectorAll('#liveBody .tl-row')]
    .map(r => r.innerText.replace(/\n/g, ' | '));
  const concl = document.getElementById('conclusion').innerText || '';
  return {
    ready: !!n,
    hidden: n ? n.classList.contains('hidden') : true,
    done: n ? n.classList.contains('done') : false,
    label: document.getElementById('liveLabel') ? document.getElementById('liveLabel').textContent : '',
    sec: document.getElementById('liveSec') ? document.getElementById('liveSec').textContent : '',
    hint: document.getElementById('liveHint') ? document.getElementById('liveHint').textContent : '',
    rows: rows,
    concl: concl.slice(0, 60),
    conclLen: concl.length
  };
})()
"""


def wait_cdp(timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
            pages = [d for d in data if d.get("type") == "page" and d.get("webSocketDebuggerUrl")]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("CDP 未就绪")


class CDP:
    def __init__(self, url):
        self.ws = connect(url, max_size=8 * 1024 * 1024)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def shot(self, path, panel="livePanel"):
        # 先把目标面板滚到顶部，否则截图只能看到输入框
        self.js(f"document.getElementById('{panel}').scrollIntoView({{block:'start'}}); 'ok'")
        time.sleep(0.15)
        r = self.call("Page.captureScreenshot", format="png")
        Path(path).write_bytes(base64.b64decode(r["data"]))


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "CX-104 从 11月8号开始误判突然变多，之前一直好好的"
    url = "http://127.0.0.1:8020/#q=" + urllib.parse.quote(msg)
    # 每次用全新 profile：复用固定目录会让浏览器吃缓存，改了前端却看到旧界面，
    # 还以为改动没生效（这个坑本轮真踩了）
    udd = Path(tempfile.gettempdir()) / f"edge-cdp-stream-{os.getpid()}"
    proc = subprocess.Popen(
        [EDGE, f"--remote-debugging-port={PORT}", f"--user-data-dir={udd}",
         "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
         "--window-size=1200,1600", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = wait_cdp()
        cdp = CDP(ws_url)
        cdp.call("Page.enable")
        cdp.call("Page.navigate", url=url)

        t0 = time.time()
        shots = {3: "ui_3s.png", 10: "ui_10s.png"}
        taken = set()
        last_label = None
        last_sig = None
        last_change = time.time()
        while True:
            el = time.time() - t0
            # 总时长不能按秒数猜：排队峰时能到 120 秒、单节点能到 130 秒，
            # 之前卡死 200 秒上限，结果在 done 帧到达前 1 秒截了中间态。
            # 改成「无进展超时」——只要界面还在推进就一直等。
            if time.time() - last_change > 240:
                print(f"[{el:5.1f}s] 240 秒没有任何进展，判定卡死，停止等待")
                break
            if el > 900:
                print(f"[{el:5.1f}s] 到达 900 秒硬上限，停止等待")
                break
            st = cdp.js(PROBE_JS) or {}
            if not st.get("ready"):
                time.sleep(1)
                continue

            sig = (st.get("label"), len(st.get("rows") or []))
            if sig != last_sig:
                last_sig = sig
                last_change = time.time()

            if st.get("done"):
                print(f"[{el:5.1f}s] *** 完成 *** {st.get('hint')}")
                break

            cur = st.get("label")
            if cur != last_label:
                print(f"[{el:5.1f}s] 正在：{cur}  （已完成 {len(st.get('rows') or [])} 步）")
                last_label = cur
            elif int(el) % 2 == 0:
                print(f"[{el:5.1f}s]   {cur} · {st.get('sec')}s   已完成 {len(st.get('rows') or [])} 步")

            for at, fn in shots.items():
                if el >= at and at not in taken:
                    taken.add(at)
                    cdp.shot(OUT / fn)
                    print(f"[{el:5.1f}s]   截图 -> {OUT / fn}")

            time.sleep(1)

        cdp.shot(OUT / "ui_final.png")
        cdp.shot(OUT / "ui_trace.png", panel="tracePanel")
        st = cdp.js(PROBE_JS) or {}
        print("\n--- 完成态 ---")
        print("hint:", st.get("hint"))
        print("结论长度:", st.get("conclLen"))
        print("步骤行:")
        for r in (st.get("rows") or []):
            print("   ", r)
        print("最终截图 ->", OUT / "ui_final.png")
        tb = cdp.js("document.getElementById('traceBody').innerHTML.slice(0,500)")
        print("\n--- traceBody 实际内容前 500 字 ---")
        print(tb)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


import urllib.parse  # noqa: E402

main()
