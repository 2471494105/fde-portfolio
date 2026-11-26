# -*- coding: utf-8 -*-
"""T23 中控台验收：用 Edge 渲染后的 DOM 与接口真数据逐项对数。

验的是「页面上的数字是不是库里的数字」，不是「好不好看」：
  C1 同端口可访问（/console 与 /app/console.html 都是 8021，不另起服务）
  C2 不引外网资源（现场断网也能开）
  C3 未处理事件数 = /v1/alerts 里 state<>closed 的条数
  C4 违约行合计 = 各判据最近一次 red_count 之和（页面不自己算别的口径）
  C5 事件流行数 = 未关闭事件数，标题逐条出现
  C6 环形图段数 = 值大于 0 的判据数，圆心数字 = 合计
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
    # T27 加了报警闭环（行类第 4 道）：环形与合计都跟着它走
    known = ["station_contract", "defect_fields", "status_policy", "alarm_ack"]
    parts = [(cid, int(latest[cid]["red_count"]) if cid in latest else 0) for cid in known]
    total = sum(v for _, v in parts)
    # 趋势线按「跑过的判据」画：行类 3 道 + 契约/值域 2 道（T26 接进事件流后才有的）
    trend_ids = known + ["data_contract", "source_values"]
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
    ok("C4 违约行合计", got_rows and got_rows.split()[0] == str(total),
       "页面 %r，接口 %d" % (got_rows, total))

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

    print("\nRESULT: %s" % ("ALL PASS" if not fails else "FAIL %d 项：%s" % (len(fails), ", ".join(fails))))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
