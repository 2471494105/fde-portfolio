"""counterpart 的可执行对账：页面上标红的那几行，与判据说红的那几行，必须是同一批工单。

为什么需要它
------------
同一条规则「关单了但看板没关」原来写在四个地方：

  1. `mock-mes/api/static/index.html` 的 `VIEWS.work_orders.flag`（行标红/橙）
  2. 同页 `refreshKpi` 的计数（第三份，连词都硬写了一遍）
  3. `mock-mes/scripts/verify_ui.py::expect_wo`（UI 期望值）
  4. `app/ds/checks.py::work_order_state_stage_conflict`（治理判据）

1、2 已合并成 `mock-mes/api/static/flag.js`（一份 JS，页面两处都调它）。剩下 3、4 是**同一规则的
两个独立实现**，谁改了谁都不会响——这正是 `DOC-58` 留下的「`counterpart` 只是文本」那条未决。

所以这里把 counterpart 拆成三个可失败的动作：

  静-1  文件与入口真的存在（`flag.js` 里 `rowFlag` / `counts` / `judge` 都是函数）
  静-2  JS 里的词（`MES_WORDS`）与客户登记的词表逐字一致——两套词表各写一份是漂移的起点
  静-3  `verify_ui.py` 的期望值里不许出现未登记的「关单/看板」词
  动     同一批行喂给两侧，按**工单号**比对集合（不比总数：总数相等可能是一换一）

为什么比工单号而不是比条数：19 条红对 19 条红，可能是两边各错了一条、刚好抵消。判据要能说出
「哪一张单两侧说法不同」，才能在评审席上被追问得住。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import status_policy as sp

REPO_ROOT = Path(__file__).resolve().parents[3]  # 06_GitHub作品仓库（agent 与 mock-mes 同仓）

_NODE_SCRIPT = """
const m = require(process.argv[1]);
const rows = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
process.stdout.write(JSON.stringify({words: m.WORDS || null, api: Object.keys(m), judge: m.judge(rows)}));
"""


def _counterparts(policy: dict) -> dict[str, dict]:
    return {cid: (spec.get("counterpart") or {}) for cid, spec in sp.checks_registry(policy).items()
            if spec.get("counterpart")}


def _node() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


def _run_js(js_path: Path, rows: list[dict] | None) -> tuple[dict | None, str]:
    """跑一侧 JS。返回 (结果, 失败原因)。跑不了必须给原因——不能当成通过。"""
    node = _node()
    if not node:
        return None, "node 不在 PATH，动态对账跑不了——跑不了就是没验，不能当通过"
    tmp = Path(tempfile.gettempdir()) / "counterpart_rows.json"
    tmp.write_text(json.dumps(rows or [], ensure_ascii=False, default=str), encoding="utf-8")
    try:
        p = subprocess.run([node, "-e", _NODE_SCRIPT, str(js_path), str(tmp)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    except subprocess.TimeoutExpired:
        return None, "node 跑 flag.js 超时（60s）"
    if p.returncode != 0:
        return None, f"node 跑 flag.js 失败（exit {p.returncode}）：{(p.stderr or '').strip()[:200]}"
    try:
        return json.loads(p.stdout), ""
    except json.JSONDecodeError:
        return None, f"flag.js 的输出不是 JSON：{(p.stdout or '')[:120]}"


def _cjk_literals(src: str, func: str) -> set[str]:
    """抓某个函数体里出现的中文字符串字面量，用来核对「期望值有没有自己另写一套词」。"""
    m = re.search(rf"def {re.escape(func)}\(\w*\):\n(.*?)\n\ndef |def {re.escape(func)}\(\w*\):\n(.*?)$",
                  src, re.S)
    body = (m.group(1) or m.group(2)) if m else ""
    return set(re.findall(r'"[^"]*[\u4e00-\u9fa5][^"]*"', body))


def counterpart_problems(policy: dict | None = None, rows: list[dict] | None = None,
                         root: Path | None = None) -> list[str]:
    """H 判据：counterpart 必须能执行。空列表表示静态三项与动态对账都成立。"""
    p = policy or sp.load()
    base = Path(root) if root else REPO_ROOT
    out: list[str] = []

    for cid, cp in _counterparts(p).items():
        if not isinstance(cp, dict):
            out.append(f"判据 {cid!r} 的 counterpart 还是纯文本——文本不执行，等于没有对账"
                       "（改成 {file, entries, words_client, allowed_tags}）")
            continue

        file = cp.get("file")
        if not file:
            # 登记成「只有说明、没有第二套代码可比」是允许的，但必须自己写清为什么不参与对账，
            # 否则「文本对照」会变成所有人偷懒的默认出口。
            if not str(cp.get("why_text_only") or "").strip():
                out.append(f"判据 {cid!r} 的 counterpart 没写 file，也没写 why_text_only"
                           "——不参与对账可以说，但得说出原因")
            continue
        js_path = base / file
        if not js_path.exists():
            out.append(f"判据 {cid!r} 的 counterpart 指向 {file}，但文件不存在"
                       "——页面已经改了，登记还指着旧路径")
            continue
        try:
            src = js_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            out.append(f"{file}：读不出 UTF-8（{e}）——读不了不等于没问题")
            continue

        entries = cp.get("entries") or []
        need = {"rowFlag", "counts", "judge"}
        if not entries:
            out.append(f"判据 {cid!r} 的 counterpart 没登记 entries——对账要对哪几个入口")
        for name in entries:
            if not re.search(rf"function {re.escape(name)}\(", src):
                out.append(f"{file}：登记了入口 {name!r}，文件里找不到 function {name}("
                           "——登记与实现对不上，对账会落空")

        # 静态-2：词必须与登记逐字一致（两套词表各写一份就是漂移的起点）
        client_name = cp.get("words_client")
        if not client_name:
            out.append(f"判据 {cid!r} 的 counterpart 没写 words_client——不知道该拿哪家客户的词表对")
        else:
            client = sp.client(client_name, p)
            spec = sp.checks_registry(p).get(cid) or {}
            js_words, why = _run_js(js_path, rows=None)
            if js_words is None:
                out.append(f"{file}：{why}")
            else:
                got = js_words.get("words") or {}
                want = {
                    # 关单词登记在判据 spec 里（T16：它属于判据的规则，不同于「看板叫法」属于客户）
                    "closedOrderLabels": list(spec.get("closed_order_labels") or []),
                    "boardClosed": list(client.get("stage_board_closed") or []),
                }
                for k, w in want.items():
                    g = list(got.get(k) or [])
                    if g != w:
                        out.append(f"{file} 的 {k} = {g}，登记的是 {w}"
                                   "——页面与判据各写一份词，改一处漏一处不会有人响")

        # 静态-3：期望值里不许自己另写一套词
        for rel in (cp.get("expectations_in") or []):
            ep = base / rel
            if not ep.exists():
                out.append(f"{rel}：登记为期望值来源，但文件不存在")
                continue
            func = cp.get("expectation_func") or "expect_wo"
            lits = _cjk_literals(ep.read_text(encoding="utf-8"), func)
            allowed = set(cp.get("allowed_tags") or [])
            spec = sp.checks_registry(p).get(cid) or {}
            if client_name:
                c = sp.client(client_name, p)
                allowed |= set(spec.get("closed_order_labels") or [])
                allowed |= set(c.get("stage_board_closed") or [])
            bad = sorted({x.strip('"') for x in lits} - allowed)
            for w in bad:
                out.append(f"{rel}::{func} 里出现未登记的词 {w!r}——期望值自己写了一套词，"
                           "页面改词它不会跟着变")

        # 动：同一批行，按工单号比集合
        if rows is None:
            continue
        res, why = _run_js(js_path, rows)
        if res is None:
            out.append(f"{file}：{why}")
            continue
        from . import checks as ck  # 延迟导入，避免模块循环

        py = ck.run_check(cid, rows=rows, name=cp.get("words_client"), policy=p)
        py_ids = {str(pr.split("：")[0]) for pr in py["problems"]}
        js_ids = {str(x) for x in (res.get("judge") or {}).get(cp.get("compares") or "red", [])}
        if py_ids != js_ids:
            only_js = sorted(js_ids - py_ids)
            only_py = sorted(py_ids - js_ids)
            out.append(f"判据 {cid!r} 与页面 {file} 对不上同一批工单："
                       f"页面多 {only_js[:6]}，判据多 {only_py[:6]}")
        extra = cp.get("extra_rules") or []
        for rule in extra:
            # 页面判得比判据多，是**登记过的差异**，不是错；但必须显式登记，否则就是悄悄漏判
            if not str(rule).strip():
                out.append(f"判据 {cid!r} 的 counterpart 里有一条空的 extra_rules——说是差异，没说差在哪")
    return out
