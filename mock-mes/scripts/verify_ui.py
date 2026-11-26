# -*- coding: utf-8 -*-
"""T9 客户端三页验收：拿 Edge 无头渲染后的 DOM，与接口返回的真数据逐行对数。

为什么不用截图判断：截图只能证明"看起来像"，证明不了"页面上那一行就是库里那一行"。
所以这里读渲染后的 DOM（--dump-dom），把每行的底色、标记、页码、页脚位置全部与
/v1/* 的返回体比对；顺便收集 stderr 里的 CONSOLE 报错。

前置：api 已在 8021 上跑着（api\run.ps1）。
用法：python -X utf8 scripts\verify_ui.py
"""
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8021"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
TMP = tempfile.gettempdir()
NOW = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
NOW_DATE = NOW[:10]

# 三个视图各自"有几列"（数据列 + 标记列），列宽改动时这里必须一起改
COLS = {"devices": 9, "defect_records": 10, "work_orders": 10, "remediation_orders": 10, "check_runs": 7}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


def render(url, tag):
    dom = os.path.join(TMP, "verify_ui_%s.html" % tag)
    err = os.path.join(TMP, "verify_ui_%s.err" % tag)
    with io.open(dom, "w", encoding="utf-8") as f, io.open(err, "w", encoding="utf-8") as e:
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--user-data-dir=" + os.path.join(TMP, "verify_ui_ud_" + tag),
                        "--virtual-time-budget=5000", "--dump-dom", url],
                       stdout=f, stderr=e, timeout=90)
    return io.open(dom, encoding="utf-8").read(), io.open(err, encoding="utf-8").read()


def text(html, tag):
    m = re.search(r'id="%s"[^>]*>(.*?)</' % tag, html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def rows(html):
    body = html.split('<tbody id="body"', 1)[1].split("</tbody>", 1)[0]
    out = []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", body, re.S):
        cls = (re.search(r'class="([^"]*)"', tr) or [None, ""])[1]
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td>(.*?)</td>", tr, re.S)]
        tags = [t[1] for t in re.findall(r'class="tag ([a-z-]+)">([^<]+)<', tr)]
        out.append({"cls": cls, "cells": cells, "tags": tags})
    return out


def btn_disabled(html, btn):
    m = re.search(r'<button id="%s"(.*?)>' % btn, html, re.S)
    return "disabled" in (m.group(1) if m else "")


def expect_defect(r):
    cls, tags = "", []
    cp = r.get("conf_pct")
    if cp is not None and (float(cp) > 100 or float(cp) < 0):
        tags.append("超范围")
        cls = "row-red"
    if (r.get("captured_at") or "") > NOW:
        tags.append("未来时间")
        cls = cls or "row-amber"
    if r.get("lot_no") in (None, ""):
        tags.append("批次 NULL" if r.get("lot_no") is None else "批次空串")
        cls = cls or "row-gray"
    return cls, tags


def expect_wo(r):
    # 关单词要认「已完成」和「已取消」两个：取消的单也是关了的单（理由见
    # fde-diagnosis-agent 的 checks spec：已合并不算，取消算）。只认「已完成」会让
    # 页面少标 9 行——这正是判据 H 第一次跑出来的差。
    if r["state"] in ("已完成", "已取消") and r["stage_id"] != "已关闭":
        return "row-red", ["关单未关板"]
    if r["stage_id"] == "已关闭" and not r["closed_at"]:
        return "row-amber", ["关板未关单"]
    return "", []


def expect_none(r):
    return "", []


def expect_run(r):
    """判据运行页：ok 绿 / warn 黄 / fail 红 / error 灰，与页面 flag 同一套语义。"""
    if r.get("status") == "ok":
        return "row-green", ["绿"]
    if r.get("status") == "warn":
        return "row-amber", ["黄"]
    if r.get("status") == "fail":
        return "row-red", ["红"]
    return "row-gray", ["脚本崩"]

def expect_rm(r):
    """整改单：closed 绿；未关闭且过期红；未关闭未过期橙。判据 J 的口径。"""
    if r.get("state") == "closed":
        return "row-green", ["已关闭"]
    due = r.get("due_on")
    if due and str(due) < NOW_DATE:
        return "row-red", ["逾期"]
    return "row-amber", ["未关闭"]


CASES = [
    ("devices", "/app/#devices", "/v1/devices?page=1&size=20", expect_none),
    ("defect_records", "/app/#defect_records", "/v1/defect_records?page=1&size=20", expect_defect),
    ("work_orders", "/app/#work_orders", "/v1/work_orders?page=1&size=20", expect_wo),
    ("remediation_orders", "/app/#remediation_orders", "/v1/remediation_orders?page=1&size=20", expect_rm),
    ("check_runs", "/app/#check_runs", "/v1/check_runs?page=1&size=20", expect_run),
    ("devices-p3", "/app/?page=3#devices", "/v1/devices?page=3&size=20", expect_none),
    ("wo-p2", "/app/?page=2#work_orders", "/v1/work_orders?page=2&size=20", expect_wo),
    ("wo-p3", "/app/?page=3#work_orders", "/v1/work_orders?page=3&size=20", expect_wo),
    ("defect-p16", "/app/?page=16#defect_records", "/v1/defect_records?page=16&size=20", expect_defect),
    ("devices-q", "/app/?q=CX-14#devices", "/v1/devices?page=1&size=20&q=CX-14", expect_none),
]

ok = True


def chk(name, got, want):
    global ok
    good = got == want
    ok = ok and good
    print(("[ok] " if good else "[!!] ") + name + " got=%r want=%r" % (got, want))


def main():
    try:
        get("/v1/devices?size=1")
    except Exception as e:
        print("接口没起在 %s（%s）。先跑 api\\run.ps1。" % (BASE, e))
        return 2
    print("本机当前时间:", NOW)
    for tag, page, api_path, exp in CASES:
        d = get(api_path)
        html, err = render(BASE + page, tag)
        rs = rows(html)
        pages = -(-d["total"] // 20)
        print("== %s  %s" % (tag, page))
        chk(tag + " total", text(html, "total"), str(d["total"]))
        chk(tag + " 行数", len(rs), len(d["items"]))
        if rs:
            ths = len(re.findall(r"<th>", html.split("</thead>")[0]))
            chk(tag + " 列数(表头/行宽)", (ths, len(rs[0]["cells"])),
                (COLS[api_path.split("?")[0].split("/")[-1]], len(rs[0]["cells"])))
        chk(tag + " 页脚", text(html, "pageinfo"),
            "第 %d–%d 条 / 共 %d 条" % ((d["page"] - 1) * 20 + 1,
                                        (d["page"] - 1) * 20 + len(d["items"]), d["total"])
            if d["items"] else "0 条")
        chk(tag + " 页码", text(html, "pageno"), "%d / %d" % (d["page"], pages))
        chk(tag + " 上一页禁用", btn_disabled(html, "prev"), d["page"] <= 1)
        chk(tag + " 下一页禁用", btn_disabled(html, "next"), d["page"] >= pages)
        chk(tag + " 着色行", [r["cls"] for r in rs], [exp(r)[0] for r in d["items"]])
        chk(tag + " 标记列", [r["tags"] for r in rs], [exp(r)[1] for r in d["items"]])
        chk(tag + " NaN 泄漏", ("NaN" in html or "undefined" in html), False)
        chk(tag + " 控制台报错", len([l for l in err.splitlines()
                                      if "CONSOLE" in l or "Uncaught" in l]), 0)

    h, _ = render(BASE + "/app/#check_runs", "kpi")
    print("侧栏计数:", text(h, "num-devices"), text(h, "num-defect_records"), text(h, "num-work_orders"), text(h, "num-remediation_orders"), text(h, "num-check_runs"))
    print("KPI:", text(h, "kpi"), "（判据运行页）")
    km = re.search(r'<span[^>]*id="kpi"[^>]*>(.*?)</span>', h, re.S)
    kpi_html = km.group(1) if km else ""
    chk("KPI 有 p95 与静默计数", "p95" in kpi_html and "静默判据" in kpi_html, True)
    m = (re.search(r'<div[^>]*id="banner"[^>]*class="([^"]*)"', h)
         or re.search(r'<div[^>]*class="([^"]*)"[^>]*id="banner"', h))
    chk("banner 未出现", (m.group(1) if m else "?").find("show"), -1)

    print("RESULT:", "ALL PASS" if ok else "HAS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())