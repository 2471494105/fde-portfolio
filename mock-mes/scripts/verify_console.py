# -*- coding: utf-8 -*-
"""T23 中控台验收：用 Edge 渲染后的 DOM 与接口真数据逐项对数。

验的是「页面上的数字是不是库里的数字」，不是「好不好看」：
  C1 同端口可访问（/console 与 /app/console.html 都是 8021，不另起服务）
  C2 不引外网资源（现场断网也能开）
  C3 未处理事件数 = /v1/alerts 里 state<>closed 的条数
  C4 确定违约行 = row_kpi 判据里 counts_in_total=true 的那部分（页面大数字只放这个），
     待口径行在备注里单列（407 = 5 确定 + 402 待口径）
  C5 事件流行数 = 未关闭事件数，标题逐条出现
  C6 环形图段数 = 值大于 0 的判据数，圆心数字 = 合计（含待确认，虚线）
  C7 趋势三条（各自最近 6 次），不是一条「合计」假线
  C8 作业页有入口链接、且中控台页标注只读

前置：api 已在 8021 上跑着，且 gov_alert 里至少有一条活事件。
用法：python -X utf8 scripts\verify_console.py
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

BASE = "http://127.0.0.1:8021"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
TMP = tempfile.gettempdir()
fails = []


def ok(name, cond, detail=""):
    print("[%s] %s%s" % ("ok" if cond else "FAIL", name, ("：" + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


def render(url, tag):
    dom = os.path.join(TMP, "verify_console_%s.html" % tag)
    err = os.path.join(TMP, "verify_console_%s.err" % tag)
    with io.open(dom, "w", encoding="utf-8") as f, io.open(err, "w", encoding="utf-8") as e:
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--user-data-dir=" + os.path.join(TMP, "verify_console_ud_" + tag),
                        "--virtual-time-budget=5000", "--dump-dom", url],
                       stdout=f, stderr=e, timeout=90)
    return io.open(dom, encoding="utf-8").read(), io.open(err, encoding="utf-8").read()


def strip(html):
    return re.sub(r"<[^>]+>", "", html).strip()


def text_of(html, tag):
    # KPI 是 div，里面还嵌着 <small>：按 id 取，闭合认第一个 </div>
    m = re.search(r'id="%s"[^>]*>(.*?)</div>' % tag, html, re.S)
    return strip(m.group(1)) if m else None


def main():
    alerts = get("/v1/alerts?size=100")
    runs = get("/v1/obs/runs?limit=40")
    obs = get("/v1/obs/summary")
    alive = [a for a in alerts["items"] if a["state"] != "closed"]

    latest = {}
    for r in runs["recent"]:
        latest.setdefault(r["check_id"], r)
    # 哪些判据算「行类」不再写在这份脚本里：由 check_rules.json 的 row_kpi 决定
    # （页面也按同一个字段过滤）。这里若再存一份名单，第三次改就会对不上。
    kpi_ids = [cid for cid, r in latest.items() if r.get("row_kpi")]
    parts = [(cid, int(latest[cid]["red_count"] or 0)) for cid in kpi_ids]
    total = sum(v for _, v in parts)
    # 407 拆成两个数：确定（counts_in_total=true）+ 待口径（false）。页面大数字只放确定的那部分。
    pending = sum(int(latest[cid]["red_count"] or 0) for cid in kpi_ids
                  if latest[cid].get("counts_in_total") is False)
    confirmed = total - pending
    # 趋势线按「跑过的判据」画：行类 + 契约/值域 2 道（T26 接进事件流后才有的）
    trend_ids = kpi_ids + ["data_contract", "source_values"]
    expect_sparks = sum(1 for cid in trend_ids if cid in latest)

    dom, err = render(BASE + "/console", "console")

    ok("C1 同端口可访问", '数据治理中控台' in dom)
    ok("C1b 无外网资源", "http://" not in dom and "https://" not in dom, "出现了外链")
    ok("C2 页面标只读", "只读账号" in dom and "raise_alerts.py --ack/--close" in dom)
    if err.strip():
        print("  [warn] stderr 非空（可能是无害的浏览器日志）：%s" % err.strip()[:120])

    got_open = text_of(dom, "kpi-open")
    ok("C3 未处理事件数", got_open and got_open.split()[0] == str(len(alive)),
       "页面 %r，接口 %d" % (got_open, len(alive)))

    got_rows = text_of(dom, "kpi-rows")
    ok("C4 确定违约行 = 只算已登记口径的那部分", got_rows and got_rows.split()[0] == str(confirmed),
       "页面 %r，确定 %d / 合计 %d" % (got_rows, confirmed, total))

    note = text_of(dom, "kpi-rows-note")
    ok("C4b 待口径行单列", pending == 0 or (note and str(pending) in note),
       "页面备注 %r，待口径 %d" % (note, pending))

    body = dom.split('id="events"', 1)[1].split("</tbody>", 1)[0]
    trs = re.findall(r"<tr[^>]*>.*?</tr>", body, re.S)
    ok("C5 事件流行数", len(trs) == len(alive), "页面 %d 行，接口 %d 条" % (len(trs), len(alive)))
    missing = [a["title"] for a in alive if a["title"] not in body]
    ok("C5b 标题逐条出现", not missing, "缺：%s" % missing)
    red_missing = [a["title"] for a in alive if a["severity"] == "red" and 'class="chip red"' not in body]
    ok("C5c 红级着色", not red_missing)

    ring = dom.split('id="ring"', 1)[1].split("</svg>", 1)[0]
    arcs = len(re.findall(r"<path ", ring))
    ok("C6 环形图段数", arcs == sum(1 for _, v in parts if v > 0), "arcs=%d" % arcs)
    ok("C6b 圆心合计", re.search(r">%d<" % total, ring) is not None)

    # 只数渲染出来的标记，不数脚本里的模板字符串（<script> 之前的部分）
    markup = dom.split("<script", 1)[0]
    spark_n = len(re.findall(r'<div class="spark">', markup))
    ok("C7 趋势条数 = 跑过的判据数", spark_n == expect_sparks,
       "spark=%d，预期 %d" % (spark_n, expect_sparks))
    ok("C7b 三条副标题写明各自次数", dom.count("各自最近 6 次") >= 1)
    # 纵轴从 0 起，1 行的变化在图上只有 0.1px；所以必须有一条文字把差值写出来，
    # 否则「修复可见」在页面上只剩数字在动、趋势不动，看的人会以为没修。
    deltas = re.findall(r'<div class="delta"[^>]*>([^<]*)</div>', markup)
    ok("C7c 每条趋势都有「较上一次」", len(deltas) == spark_n, "delta=%s" % deltas)
    ok("C7d 差值写法合法（±0 / +n / −n / —）",
       all(re.fullmatch(r"±0|[+−]\d+|—", d.strip()) for d in deltas))

    # 常见问题目录（T26）：登记几类、今天命中几类，两个数都要与接口自洽
    cat = get("/v1/issue_catalog?size=50")
    cat_body = dom.split('id="catalog"', 1)[1].split("</tbody>", 1)[0]
    cat_rows = re.findall(r"<tr[^>]*>.*?</tr>", cat_body, re.S)
    ok("C10 目录行数 = 登记类别数", len(cat_rows) == cat["total"],
       "页面 %d 行，接口 %d 类" % (len(cat_rows), cat["total"]))
    # 命中口径与页面一致：「静默」不取 alert_sweep 的 red_count（那是推送条数），
    # 取 runs.checks 里 silent 的条数；其余类别取 check_id 最近一次台账的 red_count
    silent_n = sum(1 for c in runs["checks"] if c.get("silent"))
    # 只数客户库：外部自检（scope=external）不进分子也不进分母（T27 更正过口径）
    client_cat = [c for c in cat["items"] if c.get("scope") != "external"]
    hit = 0
    for c in client_cat:
        if c["issue_id"] == "sweep_silent":
            hit += 1 if silent_n else 0
        elif c["check_id"] in latest and int(latest[c["check_id"]]["red_count"]) > 0:
            hit += 1
    got_cat = text_of(dom, "kpi-cat")
    expect_txt = "%d/%d" % (hit, len(client_cat))
    ok("C10b 问题类别 KPI = 命中/登记",
       (got_cat or "").replace(" ", "").startswith(expect_txt),
       "页面 %r，预期 %s" % (got_cat, expect_txt))
    ok("C10c 事件流有类别列", '>类别</th>' in dom)
    short_missing = [a["title"] for a in alive
                     if (a.get("detail") or {}).get("issue_short")
                     and a["detail"]["issue_short"] not in body]
    ok("C10d 事件短名逐条出现", not short_missing, "缺：%s" % short_missing)
    # 单位不许混：违约行只算行类判据；契约(件)、值域(列) 混进来，367 会变成 369
    note = text_of(dom, "kpi-rows-note")
    ok("C10e 违约行备注只列行类判据", note and "契约" not in note and "值域" not in note,
       "备注 %r" % note)

    # C10f「待确认」只属于 assumed：counts_in_total=false 里还混着「单位不是行」的判据
    # （契约按件 / 值域按列）。若把这两类也标成待确认，等于把换单位说成"没对口径"，
    # 客户会去追一个不存在的口径问题。按判据 id 逐行核，不按字符串整体核。
    def cat_row_of(issue_id: str) -> str:
        item = next((c for c in cat["items"] if c["issue_id"] == issue_id), None)
        return next((r for r in cat_rows if item and item["name"] in r), "")

    assumed_ids = {"station_contract", "alarm_ack"}
    others = [c for c in cat["items"] if c["check_id"] not in assumed_ids and c.get("check_id")]
    bad_tags = [c["issue_id"] for c in others if "待确认" in cat_row_of(c["issue_id"])]
    ok("C10f 只有 assumed 的类别标「待确认」", not bad_tags, "误标：%s" % bad_tags)

    stale = len(obs["freshness"]["stale"])
    got_fresh = text_of(dom, "kpi-fresh")
    ok("C8 陈旧表数", got_fresh and got_fresh.split()[0] == str(stale),
       "页面 %r，接口 %d" % (got_fresh, stale))

    index_dom, _ = render(BASE + "/app/index.html", "index")
    ok("C9 作业页有入口", 'href="/app/console.html"' in index_dom)

    # 明细页（T27）：中控台的每条事件标题必须能点进去，且点进去真有那几行
    links = re.findall(r'href="/problem\?alert=(\d+)"', body)
    ok("C11 事件标题都是明细页链接", len(links) == len(alive),
       "链接 %d 个，事件 %d 条" % (len(links), len(alive)))
    if alive:
        target = alive[0]
        page_dom, page_err = render(BASE + "/problem?alert=%s" % target["alert_id"], "problem")
        ok("C12 明细页能渲染出首步与改哪一栏",
           "先做这一步" in page_dom and ("改哪一栏" in page_dom),
           (page_err or "")[:80])
        ok("C12b 明细页标题与事件同名", target["title"].split("：")[0] in page_dom)
        ok("C12c 明细页行表有表头", "<thead>" in page_dom and "定位列" in page_dom)
        # 目录里带「明细」列，且外部自检类标了外部而不是给一个点不进去的链接
        ok("C12d 目录有明细列", '>明细</th>' in dom and "外部自检" in cat_body)

    # C13 白话首屏（T42）：老板先看的那一块不许出现工程术语。
    # 「首屏」按页面结构切：目录表格以上。术语下沉到目录的灰字与折叠区，
    # 不是删掉——专业的人还能点得到（这一条保护的是「不懂技术的人不卡在这」）。
    first = dom.split('<div class="panel catalog"', 1)[0]
    banned = ["判据", "口径", "台账", "契约", "值域", "自检", "事件流", "p95",
              "check_id", "SQL", "station_contract", "alarm_ack"]
    hits = [w for w in banned if w in first]
    ok("C13a 首屏无工程术语", not hits, "命中：%s" % (hits or "无"))
    # 只数卡片本身：`<div class="kpis">` 是容器，不能算进去（用空白区分，不吞 kpis 的 s）
    cards = len(re.findall(r'<div class="kpi(?:\s+[a-z]+)?"', first))
    ok("C13b 首屏只有 3 张数字卡", cards == 3, "卡片 %d 张" % cards)
    # 题面必须来自 check_rules.json 的 ask（接口下发），页面不许再抄一份文案
    asked = {}
    for r in runs["recent"]:
        if r.get("rule_ask"):
            asked.setdefault(r["check_id"], r["rule_ask"])
    missing_q = [q.get("question") for q in asked.values() if q.get("question") not in first]
    ok("C13c 客户要答的题面在首屏、且与接口一致", bool(asked) and not missing_q,
       "接口 %d 题，缺：%s" % (len(asked), missing_q or "无"))
    ok("C13d 术语与自检指标收在折叠区",
       '<details class="tech">' in dom and "kpi-p95" in dom.split('<details class="tech">', 1)[-1],
       "折叠区缺失")

    print("\nRESULT: %s" % ("ALL PASS" if not fails else "FAIL %d 项：%s" % (len(fails), ", ".join(fails))))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
