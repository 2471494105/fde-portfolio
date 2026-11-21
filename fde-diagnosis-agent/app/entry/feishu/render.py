"""帧 → 卡片 JSON 2.0。

W2 只做到"一张卡看得出整轮进展"：标题 + 进度行 + 结论 + 依据。
S1 的依据折叠、S2 的工号下拉、S3 的二次点击是 W5/W7 的事——这里不留半截分支，
点到不响应的按钮比没有按钮更糟。

卡片 2.0 的两条硬限（DOC-37 记过）：30 KB、200 个元素。这里最多是
1 个 markdown（进度）+ 分隔线 + 1 个结论 + 1 个依据 + 1 行注脚，元素数远低于上限；
字节数要到真机上量（W5），现在只是按截断上限预留。
"""

CARD_SCHEMA = "2.0"
MAX_PROGRESS = 10     # 一轮最多 12 步；多出来的折成一行"另有 N 步"
MAX_CLAIM = 900       # 结论截断：卡片里塞全文会把依据挤到折叠区外
MAX_EVIDENCE = 5


def progress_lines(frames) -> list[dict]:
    """帧 → 进度行。node_start 先占位，node_end 回填耗时与结果。

    同一个 node 会跑多轮（plan/act 循环），所以不能按 node 名去重——
    四轮 act 就是四行，现场看到"执行查询 ×4"才对得上 agent 真的查了四次。
    """
    lines: list[dict] = []
    for f in frames:
        typ = f.get("type")
        if typ == "node_start":
            lines.append({"node": f.get("node"), "label": f.get("label") or f.get("node"),
                          "ms": None, "detail": ""})
        elif typ == "node_end":
            if not _fill(lines, f):
                # 只有结束没有开始：帧层抽过之后不再假设顺序，兜住就行
                lines.append({"node": f.get("node"), "label": f.get("label") or f.get("node"),
                              "ms": f.get("ms"), "detail": f.get("detail") or ""})
    return lines


def _fill(lines, frame) -> bool:
    for ln in reversed(lines):
        if ln["node"] == frame.get("node") and ln["ms"] is None:
            ln["ms"] = frame.get("ms")
            ln["detail"] = frame.get("detail") or ""
            return True
    return False


def _progress_md(lines) -> str:
    shown = lines[-MAX_PROGRESS:]
    parts = []
    if len(lines) > MAX_PROGRESS:
        parts.append(f"另有 {len(lines) - MAX_PROGRESS} 步已折叠")
    for ln in shown:
        if ln["ms"] is None:
            parts.append(f"⏳ {ln['label']}…")
        else:
            line = f"✅ {ln['label']} · {ln['ms']} ms"
            if ln["detail"]:
                line += f"　{ln['detail']}"
            parts.append(line)
    return "\n".join(parts)


def _excerpt(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def build(frames, *, title: str = "故障诊断", footer: str = "") -> dict:
    """整轮帧 → 一张卡片。既没有 done 也没有 error 时，就是"处理中"的样子。

    只认最终帧，不做"边收边改结构"：PATCH 每次重画同一张卡，
    卡片结构稳定，出问题时只需要看一个形状。
    """
    lines = progress_lines(frames)
    done = next((f for f in frames if f.get("type") == "done"), None)
    err = next((f for f in frames if f.get("type") == "error"), None)

    elements: list[dict] = []
    if lines:
        elements.append({"tag": "markdown", "content": _progress_md(lines)})

    if err:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": "**执行中断**　" + _excerpt(err.get("message") or "", 200),
        })
        template = "red"
    elif done:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": _excerpt(done.get("final_cn") or done.get("final_text") or "", MAX_CLAIM),
        })
        # 多轮会话里 evidence 是累积的，_brief 已经标了 current；
        # 有本轮新增的就只显示本轮的，否则退化成全部（第一轮本来就全是本轮）
        evidence = [e for e in (done.get("evidence") or []) if e.get("current")] \
            or (done.get("evidence") or [])
        if evidence:
            rows = [f"- {e.get('tool_cn') or e.get('tool')}：{_excerpt(e.get('summary') or '', 80)}"
                    for e in evidence[:MAX_EVIDENCE]]
            elements.append({"tag": "markdown", "content": "**本轮依据**\n" + "\n".join(rows)})
        template = "green"
    else:
        template = "blue"

    if footer:
        elements.append({"tag": "note",
                         "elements": [{"tag": "plain_text", "content": footer}]})

    return {
        "schema": CARD_SCHEMA,
        # update_multi=true 才是共享卡片；飞书 2.0 的"能被改"挂在共享卡上
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {"elements": elements},
    }
