# -*- coding: utf-8 -*-
"""生成《视觉检测设备故障诊断 Agent · 项目介绍》PDF。

用法：python -X utf8 scripts/make_intro_pdf.py
依赖：reportlab（排版）、pymupdf（生成后自检，不属于生成路径）
素材：docs/项目介绍素材/结论页.png、轨迹页.png（界面截图裁切）
"""
import os
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET = os.path.join(ROOT, "docs", "项目介绍素材")
OUT = os.path.join(ROOT, "docs", "20261120_项目介绍.pdf")

# ---------------------------------------------------------------- 字体
FONT_DIR = r"C:\Windows\Fonts"
pdfmetrics.registerFont(TTFont("MSYH", os.path.join(FONT_DIR, "msyh.ttc"), subfontIndex=0))
pdfmetrics.registerFont(TTFont("MSYHB", os.path.join(FONT_DIR, "msyhbd.ttc"), subfontIndex=0))
pdfmetrics.registerFontFamily("MSYH", normal="MSYH", bold="MSYHB", italic="MSYH", boldItalic="MSYHB")

# ---------------------------------------------------------------- 配色
INK = colors.HexColor("#16233a")
INK_SOFT = colors.HexColor("#39465c")
ACC = colors.HexColor("#b5521d")
ACC2 = colors.HexColor("#0d6b66")
BODY = colors.HexColor("#2c3038")
MUTE = colors.HexColor("#7b818c")
LINE = colors.HexColor("#dcd8cf")
BG = colors.HexColor("#f4f2ec")
BG2 = colors.HexColor("#eef3f2")
WHITE = colors.white

# ---------------------------------------------------------------- 版式
PAGE_W, PAGE_H = A4
ML = MR = 52
MT = 46
MB = 52
CW = PAGE_W - ML - MR

S = {}
S["body"] = ParagraphStyle("body", fontName="MSYH", fontSize=9.4, leading=15.6, textColor=BODY, wordWrap="CJK", spaceAfter=3)
S["body_tight"] = ParagraphStyle("body_tight", parent=S["body"], spaceAfter=0)
S["lead"] = ParagraphStyle("lead", fontName="MSYH", fontSize=10.4, leading=17.5, textColor=INK_SOFT, wordWrap="CJK", spaceAfter=6)
S["h2"] = ParagraphStyle("h2", fontName="MSYHB", fontSize=14.5, leading=20, textColor=INK, wordWrap="CJK", spaceBefore=2, spaceAfter=8)
S["h3"] = ParagraphStyle("h3", fontName="MSYHB", fontSize=10.2, leading=15, textColor=ACC, wordWrap="CJK", spaceBefore=12, spaceAfter=5)
S["small"] = ParagraphStyle("small", fontName="MSYH", fontSize=8.2, leading=12.6, textColor=MUTE, wordWrap="CJK", spaceAfter=2)
S["cap"] = ParagraphStyle("cap", fontName="MSYH", fontSize=8.0, leading=12.4, textColor=INK_SOFT, wordWrap="CJK", spaceAfter=3)
S["bullet"] = ParagraphStyle("bullet", fontName="MSYH", fontSize=9.2, leading=14.6, textColor=BODY, wordWrap="CJK", leftIndent=11, bulletIndent=1, spaceAfter=3, bulletFontName="MSYH", bulletFontSize=9.2)
S["num"] = ParagraphStyle("num", fontName="MSYH", fontSize=9.2, leading=14.8, textColor=BODY, wordWrap="CJK", leftIndent=15, bulletIndent=0, spaceAfter=5, bulletFontName="MSYH", bulletFontSize=9.0)
S["tc"] = ParagraphStyle("tc", fontName="MSYH", fontSize=8.3, leading=12.2, textColor=BODY, wordWrap="CJK")
S["tc_b"] = ParagraphStyle("tc_b", parent=S["tc"], fontName="MSYHB", textColor=INK)
S["tc_m"] = ParagraphStyle("tc_m", parent=S["tc"], fontSize=7.9, textColor=MUTE)
S["cell_h"] = ParagraphStyle("cell_h", fontName="MSYHB", fontSize=8.3, leading=12.2, textColor=WHITE, wordWrap="CJK")
S["mono"] = ParagraphStyle("mono", fontName="MSYH", fontSize=8.3, leading=13.6, textColor=INK, wordWrap="CJK")

# 封面用
S["kicker"] = ParagraphStyle("kicker", fontName="MSYH", fontSize=9, leading=13, textColor=colors.HexColor("#c9d2e0"), wordWrap="CJK")
S["title"] = ParagraphStyle("title", fontName="MSYHB", fontSize=27, leading=38, textColor=WHITE, wordWrap="CJK")
S["sub"] = ParagraphStyle("sub", fontName="MSYH", fontSize=10.4, leading=18, textColor=colors.HexColor("#dfe5ee"), wordWrap="CJK")
S["meta"] = ParagraphStyle("meta", fontName="MSYH", fontSize=8, leading=13.4, textColor=colors.HexColor("#aab5c6"), wordWrap="CJK")
S["num_big"] = ParagraphStyle("num_big", fontName="MSYHB", fontSize=15.5, leading=20, textColor=INK, wordWrap="CJK")
S["num_lbl"] = ParagraphStyle("num_lbl", fontName="MSYH", fontSize=7.6, leading=11, textColor=MUTE, wordWrap="CJK")


def P(t, style="body"):
    return Paragraph(t, S[style])


def bullets(items):
    return [Paragraph(t, S["bullet"], bulletText="·") for t in items]


def tbl(rows, widths, style_extra=None, header=True, pad=5.5, valign="TOP"):
    data = []
    for i, row in enumerate(rows):
        cells = []
        for j, cell in enumerate(row):
            if isinstance(cell, str):
                st = "cell_h" if (header and i == 0) else ("tc_b" if j == 0 and header else "tc")
                cells.append(Paragraph(cell, S[st]))
            else:
                cells.append(cell)
        data.append(cells)
    t = Table(data, colWidths=widths, hAlign="LEFT")
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), valign),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
    ]
    if header:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), INK),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
        ]
    if style_extra:
        cmds += style_extra
    t.setStyle(TableStyle(cmds))
    return t


def hr(space_before=4, space_after=6):
    return Spacer(1, space_before) if False else Table([[""]], colWidths=[CW], rowHeights=[0.6],
                                                        style=TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, LINE),
                                                                          ("LEFTPADDING", (0, 0), (-1, -1), 0)]))


def plate(flowables, bg=BG, pad=13, width=CW, accent=ACC):
    """底色信息块：整块铺底色 + 左侧一道 3pt 竖色条。"""
    return Table([[flowables]], colWidths=[width], hAlign="LEFT",
                 style=TableStyle([
                     ("BACKGROUND", (0, 0), (-1, -1), bg),
                     ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
                     ("VALIGN", (0, 0), (-1, -1), "TOP"),
                     ("LEFTPADDING", (0, 0), (-1, -1), pad + 2),
                     ("RIGHTPADDING", (0, 0), (-1, -1), pad),
                     ("TOPPADDING", (0, 0), (-1, -1), pad),
                     ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
                 ]))


# ---------------------------------------------------------------- 架构图
def arch_drawing():
    d = Drawing(CW, 158)
    boxes = [
        (0, 74, "现场一句话"),
        (88, 74, "判意图"),
        (176, 116, "判断下一步查什么 / 执行查询"),
        (306, 74, "核对依据"),
        (394, 88, "整理结论"),
    ]
    y, h = 100, 32
    for (x, w, label) in boxes:
        d.add(Rect(x, y, w, h, fillColor=BG, strokeColor=LINE, strokeWidth=0.6))
        size = 7.6 if w < 100 else 7.4
        d.add(String(x + w / 2.0, y + h / 2.0 - 3.4, label, fontName="MSYH", fontSize=size,
                     fillColor=INK, textAnchor="middle"))
    for x0, x1 in [(74, 88), (162, 176), (292, 306), (380, 394)]:
        cy = y + h / 2.0
        d.add(Line(x0 + 1, cy, x1 - 4, cy, strokeColor=INK_SOFT, strokeWidth=0.8))
        d.add(Polygon([x1 - 4, cy - 2.6, x1 - 4, cy + 2.6, x1, cy], fillColor=INK_SOFT, strokeColor=INK_SOFT))

    # 循环回路
    top = y + h + 8
    d.add(Line(292, y + h, 292, top, strokeColor=ACC, strokeWidth=0.9))
    d.add(Line(292, top, 176, top, strokeColor=ACC, strokeWidth=0.9))
    d.add(Line(176, top, 176, y + h + 3, strokeColor=ACC, strokeWidth=0.9))
    d.add(Polygon([172.6, y + h + 3, 179.4, y + h + 3, 176, y + h], fillColor=ACC, strokeColor=ACC))
    d.add(String(234, top + 4, "循环，直到证据足够或步数预算耗尽", fontName="MSYH", fontSize=7, fillColor=ACC,
                 textAnchor="middle"))

    # 旁支
    side_y, side_h = 26, 26
    sides = [(88, 74, "查设备档案"), (176, 74, "查手册"), (264, 74, "追问补充信息")]
    for (x, w, label) in sides:
        d.add(Rect(x, side_y, w, side_h, fillColor=WHITE, strokeColor=LINE, strokeWidth=0.6))
        d.add(String(x + w / 2.0, side_y + side_h / 2.0 - 3.2, label, fontName="MSYH", fontSize=7.3,
                     fillColor=INK_SOFT, textAnchor="middle"))
        d.add(Line(125, y, x + w / 2.0, side_y + side_h, strokeColor=colors.HexColor("#9aa2ae"), strokeWidth=1.2,
                   strokeDashArray=[2.6, 2.2]))
    return d


# ---------------------------------------------------------------- 内容
def cover():
    st = [Spacer(1, 84)]
    head = [
        P("项目介绍　·　工业 Agent　·　2026 年 11 月", "kicker"),
        Spacer(1, 10),
        P("视觉检测设备故障诊断 Agent", "title"),
        Spacer(1, 8),
        P("一句话说清异常现象，它自己决定先查哪里、把每一步的依据摆出来，<br/>并在授权边界内把工单写进系统。", "sub"),
        Spacer(1, 12),
        P("LangGraph 编排　·　deepseek-chat（官方端点）　·　PostgreSQL + pgvector 单库　·　FastAPI 单页界面　·　可离线复跑", "meta"),
    ]
    headplate = Table([[head]], colWidths=[CW], hAlign="LEFT",
                      style=TableStyle([
                          ("BACKGROUND", (0, 0), (-1, -1), INK),
                          ("LEFTPADDING", (0, 0), (-1, -1), 24),
                          ("RIGHTPADDING", (0, 0), (-1, -1), 24),
                          ("TOPPADDING", (0, 0), (-1, -1), 26),
                          ("BOTTOMPADDING", (0, 0), (-1, -1), 28),
                      ]))
    st += [headplate, Spacer(1, 16)]

    read = [
        P("这份文档怎么读", "h3"),
        P("<b>只想知道这是什么、靠不靠得住</b>　读第 2 页的一页速览，两分钟够。", "body_tight"),
        P("<b>想追问怎么做的、哪里可能出错</b>　第 4–9 页是能力实录、架构、工程决定、基线数字与已知缺口。", "body_tight"),
        P("<b>想自己跑一遍</b>　最后一章有起服务、跑评测、复位数据的三条命令。", "body_tight"),
    ]
    st += [plate(read, bg=BG, pad=14), Spacer(1, 22)]

    nums = [
        ("26/26", "单次全量用例通过"),
        ("98.4%", "结论明确出处率"),
        ("8", "可调用工具"),
        ("289", "单元测试条目"),
        ("45.9 s", "三幕演示总耗时"),
        ("1000", "误判日志条数"),
    ]
    cells = [[P(n, "num_big"), P(l, "num_lbl")] for n, l in nums]
    rows = [cells[0:3], cells[3:6]]
    numtab = Table([[c for c in r] for r in rows], colWidths=[CW / 3.0] * 3, hAlign="LEFT")
    # 每格拆成两行（数字在上、说明在下）
    data = []
    for r in rows:
        data.append([[c[0], c[1]] for c in r])
    numtab = Table(data, colWidths=[CW / 3.0] * 3, hAlign="LEFT",
                   style=TableStyle([
                       ("BACKGROUND", (0, 0), (-1, -1), BG),
                       ("VALIGN", (0, 0), (-1, -1), "TOP"),
                       ("LEFTPADDING", (0, 0), (-1, -1), 12),
                       ("TOPPADDING", (0, 0), (-1, -1), 9),
                       ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                       ("LINEAFTER", (0, 0), (-1, -1), 0.6, WHITE),
                       ("LINEBELOW", (0, 0), (-1, -1), 0.6, WHITE),
                   ]))
    st += [numtab, Spacer(1, 22)]

    st += [P("关于这份材料的事实边界", "h3")]
    st += [plate([P("本文档描述的是一个<b>构造的假设项目</b>。设备型号、厂商文档结构与参数名取自公开手册"
                    "（Cognex In-Sight 2800、Keyence CV-X450、CA-DC21E 等），可以查证；产线数据、故障分布、"
                    "工单内容是按「每类故障必须具备的证据特征」反推构造的。它验证的是架构与工程做法，"
                    "不代表任何真实工厂的真实数据。", "small"),
                 Spacer(1, 6),
                 P("全文 11 页：一页速览 → 场景 → 输出实录 → 工具轨迹 → 架构与工具 → 三个工程决定 → "
                   "评测基线 → 已知限制 → 复现 → 术语", "small"),
                 Spacer(1, 4),
                 P("版本 2026-11-20　·　评测基线 eval/report_20261120_1149_1f5671e.json　·　"
                   "文中所有数字都能在该报告或仓库内文档里找到对应位置", "small")], bg=BG, accent=INK_SOFT, pad=14)]
    return st


def page_overview():
    st = [P("一页速览", "h2")]
    st += [P("这是一个<b>分流判断</b>型的现场 Agent，不是问答机器人。它的正确答案不是一段文本，"
             "而是一个方向判断，并且必须能被下一个人复核。", "lead")]

    st += [plate([
        P("它是什么", "body_tight"),
        Spacer(1, 3),
        P("输入是操作员一句现场话，例如「CX-101 误判率有点高，你看看怎么回事」。"
          "它自己决定查哪个系统、查几次、要不要反问；输出是根因判断、处置动作、每条数字的出处，"
          "以及哪里必须转人工。", "body_tight"),
    ], bg=BG2, accent=ACC2)]
    st += [Spacer(1, 12)]

    st += [plate([
        P("它解决什么问题", "body_tight"),
        Spacer(1, 3),
        P("误判率飙升时，第一反应通常是「模型不准了」。但现场真相常在光源衰减、镜头污染、标定漂移或来料批次里。"
          "判错方向的代价是停机两小时，或者白调两天阈值。这个项目把「该往哪查」变成一次有据可查的判断。", "body_tight"),
    ], bg=BG2, accent=ACC2)]
    st += [Spacer(1, 12)]

    st += [P("它交付四种能力", "h3"), KeepTogether(bullets([
        "<b>诊断</b>：按工位、批次、时间聚合误判记录，与光源寿命、标定漂移、告警码交叉定位。",
        "<b>查档</b>：读设备台账；台账里没有的设备直接拒答并列出可选项，不对不存在的设备做任何推断。",
        "<b>查手册</b>：550 块厂内文档检索，并对跨文档的参数冲突主动上报，不擅自裁决。",
        "<b>执行</b>：只在授权范围内写「待人工确认」的工单草稿，建单成功的唯一依据是回读结果。",
    ]))]

    st += [P("凭什么可信（下面四个数字都能在评测报告里指回去）", "h3"), KeepTogether(bullets([
        "结论里每个数字都带出处：工具名 + 参数 + 字段，当前基线 <b>308/313 = 98.4%</b> 明确出处。",
        "26 条回归用例单次全量 <b>26/26</b>；但满分只出现过两轮，所以不宣称「稳定 100%」。",
        "<b>289 条单测</b>钉住护栏：终态词、引用、幂等、步数预算、JSON 三态。",
        "已知缺口单独成章写出来，不藏在脚注里。",
    ]))]

    st += [Spacer(1, 12),
           plate([P("与检索问答的差别", "body_tight"), Spacer(1, 3),
                  P("检索问答的正确答案是一个文档片段；这里的正确答案是一次判断——同一个现象，误判集中在固定工位指向局部硬件，"
                    "全工位随机指向共用阈值。路径由证据决定，不由流程图决定。", "body_tight")], bg=BG, accent=ACC)]
    st += [Spacer(1, 12)]

    st += [P("它刻意不做的三件事", "h3"), KeepTogether(bullets([
        "<b>不接真机</b>：不控 PLC、不改设备参数，不产生任何物理动作。",
        "<b>不替人决策</b>：结论是建议，工单是待确认草稿，都要人点确认。",
        "<b>不吹稳定性</b>：通过率一律标 pass@1 与四轴指纹，不把一次全量说成稳定能力。",
    ]))]

    st += [Spacer(1, 12),
           plate([P("这三句话不用信，翻页就能自己对一遍：任一数字都能按方括号里的工具名与参数，在第 5 页的轨迹表里找到同一次调用的返回字段；"
                    "「误判集中在固定工位」与「全工位随机分布」走的是两条不同的查询路径；建单成不成看回读，不看状态码。"
                    "换成客户验收的说法就是三行：任务完成率 26/26、引用命中 98.4%（可核验 98.7%）、异常兜底全部拒答。",
                    "small")], bg=BG2, accent=ACC2, pad=10)]
    return st


def page_scene():
    st = [P("场景：一个构造的现场，和三类真实痛点", "h2")]
    st += [P("设定是我补的，规则只有一条：让每一类故障在数据里都有可区分的证据特征，"
             "否则「模型判对」和「数据碰巧」就分不开。", "lead")]

    rows = [
        ["项", "设定"],
        ["工厂", "某精密结构件厂，外观与尺寸全检，日节拍按秒计"],
        ["线体", "3 条线、5 个视觉工位（CX-101 … CX-105），每线 1–3 个工位"],
        ["设备", "Cognex In-Sight 2800 / Keyence CV-X450 + CA-H200CX；16 mm 镜头；CA-DC21E 光源"],
        ["数据源", "设备台账、误判日志 1000 条、标定记录 28 次、光源亮度史 7 条、告警字典 8 条、工单系统、厂内文档 550 块"],
        ["现状", "夜班误判率高于白班；三条线各自有一套处置习惯，口径不统一"],
    ]
    st += [tbl(rows, [52, CW - 52])]
    st += [Spacer(1, 12)]

    st += [plate([
        P("一次夜班事故（构造）", "h3"),
        P("某月夜班，CX-101 的误判率从 10% 升到 23.89%。一线怀疑阈值，质量怀疑来料批次，设备怀疑光源老化。"
          "三方各查一半，停线四小时，最后发现是光源累计点亮 8620 小时（额定 8000）、亮度被两次下调——"
          "一个五分钟能查清的事实，被四个系统的边界挡住了。", "body_tight"),
    ], bg=BG, accent=ACC)]
    st += [Spacer(1, 12)]

    st += [P("三类痛点", "h3"), KeepTogether(bullets([
        "<b>证据分散在四个系统里</b>：误判日志在 MES 侧、标定在设备科、光源小时数在维护记录、口径在手册和厂内 SOP。"
        "人会漏，漏掉的那一项往往正是根因。",
        "<b>口径冲突没人裁</b>：手册写光源亮度 80，厂内 SOP 写 65；标定周期 90 天对 30 天。"
        "谁都能引一份对自己有利的。",
        "<b>判断和动作混在一起</b>：一着急就先建单，把「派工」当成「结论」，返工率因此上升。",
    ]))]

    st += [P("所以选题是「分流」，不是「检索」", "h3")]
    st += [P("如果只需要查手册，一个检索就够了。难的是同一个现象有两种都成立的解释：误判集中在 1 号工位，"
             "指向局部硬件（镜头、光源、标定各自独立）；全工位随机分布，指向共用阈值或模型漂移。"
             "这个判断交给模型做，代码里不写基于数值的 if-else——这是它区别于固定工作流的全部。", "body")]
    st += [P("数据里怎么埋证据（这个项目最花时间的部分）", "h3")]
    rows = [
        ["故障类型", "数据里必须同时成立的特征"],
        ["光源衰减", "累计点亮小时超过额定寿命，且亮度设定被人为下调过"],
        ["镜头污染", "误判率随运行时间单调上升，无告警，标定漂移极小"],
        ["标定漂移", "漂移量超阈值，且发生时间与误判起点吻合"],
        ["来料批次", "误判集中在同一批次号，颜色或尺寸分布偏移"],
        ["阈值过严", "置信度分布整体下移，硬件指标全部正常"],
        ["模型漂移", "长期缓慢上升，无任何硬件异常"],
    ]
    st += [tbl(rows, [76, CW - 76], pad=4.4)]
    st += [Spacer(1, 6)]
    st += [P("这些分布是按特征反推造出来的：好处是每条用例的期望结论都有据可查，代价是真实产线的分布不会这么配合。"
             "所以后面引用数字时我一直说「架构能力」，不说「诊断准确率」。", "small")]
    return st


def page_output():
    st = [P("输出长什么样：一次真实运行的结论页", "h2")]
    st += [P("下面是 2026-11-20 13:20 那次运行的原样截图（CX-101，6 次工具，14.2 s）。界面已做中文化："
             "工具名、参数名、引用标签全部中文，原始英文串只留在折叠的「工程核查」区，因为模型当时看到的就是那份文本。", "lead")]
    img = Image(os.path.join(ASSET, "结论页.png"), width=CW, height=CW * 700.0 / 1065.0)
    st += [img, Spacer(1, 6)]
    st += [P("图 1　诊断结论区。截图来自本地 Web 界面，未做任何美化或重排。", "small")]
    st += [Spacer(1, 8)]
    st += [KeepTogether([
        P("第一句就是判断，不是过程复述：<b>「1 号工位误判高度集中，叠加光源累计点亮时长已超额定寿命、亮度被两次下调，"
          "根因判断为光源衰减」</b>，并主动补一句「标定侧已排除，另有两处文档口径冲突需人工确认」。", "bullet"),
        P("每个数字后面挂着方括号出处，例如「8620 小时 [查光源使用]」「23.89%（113 条中 27 条）"
          "[查误判记录{分组维度=按工位、按缺陷类型、按批次、按天}]」。读者可以按工具和参数复现那一次查询。", "bullet"),
        P("遇到两份文档口径不一致，它写「本次不替您选定」，把冲突作为结论的一部分交付，而不是悄悄挑一个用。", "bullet"),
    ])]
    st += [Spacer(1, 10)]

    st += [P("<b>这次运行的账</b>", "body_tight"), Spacer(1, 3)]
    rows = [
        ["项", "值"],
        ["本轮新查", "6 次工具调用，涉及 6 个不同工具"],
        ["结论里的数字", "12 个，全部带出处（界面上标为「引用 12/12」）"],
        ["耗时", "14.2 s（单次，含模型往返）"],
        ["结论结构", "判断 → 依据（逐条数字）→ 处置动作 → 待人工确认项"],
    ]
    st += [tbl(rows, [96, CW - 96], pad=4.6, header=True)]
    st += [Spacer(1, 6)]
    st += [P("这张表不是事后统计，就是界面顶部那行本来显示的内容——之所以把它写进文档，"
             "是因为「跑了多久、查了几次、引了多少个数字」是判断一次诊断值不值得信的三个最小指标。", "small")]
    return st


def page_trace():
    st = [P("它读了什么：工具轨迹是证据链的另一半", "h2")]
    st += [P("同一个页面往下滚，是「工具调用轨迹」。每一步的工具名、参数、返回字段都展开成表格，"
             "原始 JSON 折叠在「工程核查用」里——删了它就没法回答「模型到底读到了什么」。", "lead")]
    img = Image(os.path.join(ASSET, "轨迹页.png"), width=CW * 0.92, height=CW * 0.92 * 830.0 / 965.0)
    st += [img, Spacer(1, 6)]
    st += [P("图 2　轨迹区。参数、返回、分组明细逐项落在表里，数字与结论区一一对应。", "small")]
    st += [Spacer(1, 6)]
    st += [KeepTogether([
        P("<b>参数可见</b>：设备编号 CX-101；分组维度「按工位、按缺陷类型、按批次、按天」——多维聚合一次调用完成。", "bullet"),
        P("<b>返回可见</b>：设备编号、查询状态、产线、工位、相机型号、镜头、光源型号、保养周期逐字段落到表里。", "bullet"),
        P("<b>明细可见</b>：1 号工位 34 条中误判 20（58.8%），3 号工位 47 条中 5 条（10.6%）。"
          "同一台设备两个工位差五倍，这就是「局部硬件」这个方向判断的来源。", "bullet"),
    ])]
    st += [Spacer(1, 12)]

    st += [plate([
        P("工具也会给出错误的安心感", "body_tight"),
        Spacer(1, 3),
        P("问 CX-106（台账里没有这台设备）时，query_defect_log 的返回是 no_defect_log——"
          "「设备确实没有误判日志，属于结论而非故障」。这句话读起来像「设备没问题」，但它只是「查不到记录」。"
          "模型没有顺着这句话说下去，仍以设备台账为准，拒答并列出 CX-101…CX-105 五个可选项。"
          "工具措辞不能当结论用，这是要靠交叉验证挡住的一类错误。", "body_tight"),
    ], bg=BG2, accent=ACC2)]
    return st


def page_arch():
    st = [P("架构：一条主链、三层岔口，不写死路径", "h2")]
    st += [arch_drawing(), Spacer(1, 2)]
    st += [P("图 3　主链是 判意图 →（诊断）判断下一步查什么 与 执行查询（循环）→ 核对依据 → 整理结论。"
             "图为简化：查档、查手册、追问各自成支，ask_user 与 END 省略。", "small")]
    st += [Spacer(1, 6)]

    rows = [
        ["岔口", "谁在判断", "判什么", "代码里留了什么"],
        ["L1 意图", "规则前置 + 模型", "诊断 / 查档 / 查手册 / 执行 / 拒答 / 追问", "只有代价分档，没有置信阈值"],
        ["L2 方向", "全部交给模型", "硬件侧，还是算法侧", "零数值分支：只看有没有 tool_calls"],
        ["L3 冲突", "工具驱动", "多源取值不一致时必须上报", "不允许自行挑选权威版本"],
    ]
    st += [tbl(rows, [50, 96, 150, CW - 296])]
    st += [Spacer(1, 8)]

    st += [P("为什么这不算 workflow", "h3")]
    st += [P("workflow 是「先 A 再 B 再 C」。这里的路径由证据决定：同一句「误判变多」，如果误判集中在固定工位，"
             "它去查光源与标定；如果全工位随机分布，它去查阈值与批次。两次运行的路径可以不同而都成立——"
             "这不是不稳定，这是判断。判据是：这些分岔点一旦写成 if-else，就等于把「看数据下判断」换成了「照流程图执行」。", "body")]

    st += [P("八个工具，一条工具链", "h3")]
    st += [P("工具不追求多，追求「每个方向判断都有数据支撑」。八个工具里只有最后一个会写库，其余全部只读。", "small")]
    rows = [
        ["工具（界面中文名）", "做什么", "值得注意的一点"],
        ["query_device_profile　查设备档案", "台账、型号、保养周期", "不存在的设备明确报「不存在」，不推断"],
        ["query_defect_log　查误判记录", "按 hour / day / station / defect_type / batch 聚合", "支持逗号多维同查，方向判断的主证据"],
        ["query_calibration　查标定记录", "漂移量、趋势、距上次天数", "用来排除或确认标定漂移"],
        ["query_light_usage　查光源使用", "累计小时、额定寿命、亮度史", "光源衰减的判据来源"],
        ["search_manual　查手册", "550 块文档，768 维向量检索", "每条返回带来源文档与位置"],
        ["check_doc_conflict　查文档冲突", "同一参数在多份文档取值不同", "只上报，不裁决"],
        ["query_work_order　查工单", "只读，含历史同类工单", "防重复派工"],
        ["create_work_order　建工单", "写 mock QMS，带幂等键", "成功唯一依据是回读，不是状态码"],
    ]
    st += [tbl(rows, [146, 168, CW - 314], pad=4.0)]
    st += [Spacer(1, 8)]
    st += [P("三条硬边界（都在代码里，不是提示词里的建议）：<b>步数预算</b> 单次 12 步，到顶强制收口、不再发起新查询；"
             "<b>工具白名单</b> 只有登记过的 8 个工具进 schema，模型调不出别的东西；"
             "<b>写操作闸门</b> 意图必须是「执行动作」，否则建单被驳回并落驳回码。", "small")]
    return st


def page_engineering():
    st = [P("三个值得单独讲的工程决定", "h2")]

    st += [P("一、引用式溯源：让每个数字都能被追回去", "h3")]
    st += [KeepTogether(bullets([
        "结论里每个数字必须带引用：工具名 + 参数指纹 + 字段路径，而不是「根据资料显示」。",
        "校验层逐条核对两件事：这个数字在工具返回里找得到吗（可核验率 98.7%）；引文与原文逐字一致吗（0 处不符）。",
        "找不到出处的数字<b>不删掉</b>，标灰保留，并把「未溯源数字条数」写进报告——删掉不等于修正，"
        "反而把「引用错了」换成了「没有引用」。",
    ]))]

    rows = [
        ["指标", "口径", "当前基线"],
        ["明确出处", "每个数字能指到具体工具与参数", "308/313 ＝ 98.4%"],
        ["可核验", "出处能在该次工具返回里逐字找到", "309/313 ＝ 98.7%"],
        ["引文核对", "引用的原文与工具返回逐字比对", "16 处合格，0 处不符"],
        ["未溯源", "整条结论里没有任何出处的数字", "2 条用例各 1 个，都是区间陈述（标灰放行）"],
    ]
    st += [tbl(rows, [64, 268, CW - 332], pad=4.2)]
    st += [Spacer(1, 5)]
    st += [P("<b>踩过的坑</b>　有一条用例的引用指错了位置，模型回环后把它改成了 null：指错消失了，"
             "但结论里多出一个没有出处的数字。判据抓的正是这个反向变化——把指错的引用改成 null 不是修正，"
             "是把「引用问题」换成了「没有引用」。那条因此判失败，而不是放过。", "small")]

    st += [P("二、执行边界：推荐不是指令", "h3")]
    st += [KeepTogether(bullets([
        "建单写的是「待人工确认的工单草稿」，不是派工指令；诊断结论里的人话建议，系统不当成动作执行。",
        "建单成功的唯一依据是回读：工单号真的出现在库里、状态真的是「待处理」。HTTP 200 不算。",
        "幂等键 ＝ 设备 + 故障日 + 根因。重复调用返回 deduped=True，不当成新单，避免一次事故派两张单。",
        "禁终态词：不允许出现「已修复」「问题解决」这类无法自证的措辞。",
    ]))]
    st += [Spacer(1, 4)]
    st += [P("<b>为什么工单系统要先做成 mock</b>　真实 QMS 有个坏习惯：HTTP 200，业务失败码藏在 body 里。"
             "只判断状态码，「建单成功」会在业务上根本不成立。mock 把这个坑原样复现，代码就必须用回读确认。", "small")]
    st += [P("<b>踩过的坑</b>　有用户只说「CX-101 有点问题，你查一下」，模型跑了 12 步之后自己把单建了——"
             "用户没要求建单，这是越界。加写意图闸之后，同一句话以 10 步收口、不建单；"
             "而明确要求建单的场景仍然 4 步完成。", "small")]

    st += [P("三、评测与留档：数字必须能横着比", "h3")]
    st += [KeepTogether(bullets([
        "26 条用例覆盖硬件、算法、信息不全、建单、陷阱五组，其中 3 条是多轮。",
        "报告头记四轴指纹：code_hash、cases_hash、seed_hash、commit。缺任何一项，这个通过率就不能和别的批次比。",
        "判据失败与模型失败分账记（owner ＝ model / env / uncertain）。不分开，「判据误伤」会永远藏在模型失败里。",
    ]))]
    st += [Spacer(1, 4)]
    st += [P("<b>踩过的坑</b>　code_hash 最初只算 4 个文件，不含图拓扑与判据脚本。结果「修了一个崩溃」和"
             "「改了判据口径」两件事 hash 都不动，两份报告指纹相同、实际不可比。改成按「改完之后同一批用例的"
             "数字会不会变」来定 hash，才真正钉住。", "small")]
    st += [Spacer(1, 12)]
    st += [plate([P("这三件事的共同点", "body_tight"), Spacer(1, 3),
                  P("它们都不是「能不能跑通」的问题，而是「跑通了算不算数」的问题。判据、边界、指纹，"
                    "都是为了让「一次成功」变成「可复核的成功」。", "body_tight"),
                 Spacer(1, 4),
                 P("如果只能保留一项，我保留引用式溯源：判断错了人还能改，数字错了人不知道该改哪里。", "body_tight")],
                 bg=BG, accent=ACC)]
    return st


def page_metrics():
    st = [P("可信度：一份可以引用的基线", "h2")]
    st += [P("报告在 eval/report_20261120_1149_1f5671e.json，单进程串行跑完 26 条。下面只抄它 summary 与 meta 里的字段，"
             "不做换算、不做四舍五入以外的加工。", "lead")]

    rows = [
        ["项", "值"],
        ["用例", "26 条（含 3 条多轮），单次全量 26/26"],
        ["总耗时", "333.3 s，平均 12.8 s/条（含核对轮与重试）"],
        ["模型与端点", "deepseek-chat　·　api.deepseek.com（官方端点）"],
        ["语义正确 / 工具选择", "26/26　·　26/26"],
        ["明确出处率", "308/313 ＝ 98.4%"],
        ["可核验率", "309/313 ＝ 98.7%"],
        ["引文核对", "16 处合格　·　0 处不符"],
        ["未溯源数字", "2 条用例各 1 个（区间陈述，标灰放行）"],
        ["JSON 解析失败", "0/26（一次重试后 fail closed）"],
        ["步数预算耗尽", "0/26"],
        ["单元测试", "289 条通过（0.21 s）"],
        ["四轴指纹", "code_hash cd9a0a36bce3　·　cases_hash 4c9bd0d53ec4　·　seed_hash 9caa1322fbfc　·　commit 1f5671e"],
    ]
    st += [tbl(rows, [92, CW - 92], pad=4.2)]
    st += [Spacer(1, 10)]

    st += [plate([
        P("三句必须一起说的话", "body_tight"),
        Spacer(1, 3),
        P("① 26/26 是 <b>pass@1</b>：每条只跑一次，不等于稳定性。", "body_tight"),
        P("② 满分只出现过两轮，中间隔了一次判据改造，所以不宣称「稳定 100%」。", "body_tight"),
        P("③ 四道护栏（解析三态、终态词核对、种子覆盖、四轴校验）本轮一次都没有拦住东西；"
          "它们的价值目前只有单测的正反证据，不能说成「修好了所有问题」。", "body_tight"),
    ], bg=BG, pad=12)]
    st += [Spacer(1, 10)]

    st += [P("模型选型的教训（W0 阶段：3 个工具、5 条用例、粗判据）", "h3")]
    rows = [
        ["模型", "通过率", "当时看到的真相"],
        ["本地 qwen2.5:7b", "100%", "低标准满分：2 步就停手，省下的 token 是没干的活"],
        ["官方 deepseek-chat", "73.3%", "证据不全但结论对，慢在补证据"],
    ]
    st += [tbl(rows, [104, 66, CW - 170], pad=4.5)]
    st += [Spacer(1, 6)]
    st += [P("两个数字都不能直接读。这次对照之后，我把判据本身当成评测对象：先证明判据能分辨对错，再谈模型的通过率。", "body")]
    st += [Spacer(1, 6)]

    st += [P("这份报告怎么读（也是我要求别人引用我数字时的三条）", "h3"), KeepTogether(bullets([
        "报数字必须同时报三样：<b>cases_hash</b>（夹具版本）、<b>code_hash</b>（代码版本）、<b>evaluated</b>（跑了几条）。"
        "缺任何一样，这个数就只能当自测，不能横着比。",
        "环境失败单独归因（owner ＝ env）：mock QMS 没起来造成的那条红，不算模型答错，也不会被算成通过。",
        "判据的锅与模型的锅分开记：判据误伤不修掉，模型真不会就永远混在里面看不出来。",
    ]))]
    return st


def page_limits():
    st = [P("已知限制与取舍", "h2")]
    st += [P("以下是可以被追问的清单，按「会不会影响看演示的人做判断」分成两组。这些不是待办事项的委婉说法，"
             "是当前版本的边界。", "lead")]

    st += [P("A　会影响对能力的判断", "h3")]
    items_a = [
        "<b>语义正确率仍是关键词与结构事实的「或门」，不是 LLM 打分。</b>它能证明「提到了」「记对了事实」，"
        "不能证明「答得好」。换成 LLM 打分会让全部历史基线作废、要重跑，演示前不动——这是取舍，不是没发现。",
        "<b>满分只出现两轮。</b>当前 26/26 不能外推成稳定能力；复杂现象上 12 步预算仍可能撞顶"
        "（本轮预算耗尽 0 条，不代表不会）。",
        "<b>知识类遇到「文档没给标准值」会停在「手册没给」。</b>它没把「两处口径冲突」本身当成一种可交付的答案。",
    ]
    for i, t in enumerate(items_a, 1):
        st += [Paragraph(t, S["num"], bulletText="%d" % i)]
    st += [Spacer(1, 4)]

    st += [P("B　真实存在，但不影响看演示", "h3")]
    items_b = [
        "<b>工单系统是本地 mock。</b>真实 QMS 的鉴权、限流、字段映射没有验证过。",
        "<b>只有 create 一条写路径</b>，没有撤回、改派、关闭。",
        "<b>幂等键按天算</b>：跨天的同一现象会重复建单。",
        "<b>平均 12.8 s/条</b>，对「现场秒级响应」不够；换本地部署另有 25% 量级的质量损失。",
        "<b>数据是构造的</b>：故障分布按证据特征反推，数字反映架构能力，不反映真实产线分布。",
    ]
    for i, t in enumerate(items_b, 4):
        st += [Paragraph(t, S["num"], bulletText="%d" % i)]

    st += [Spacer(1, 8)]
    st += [plate([P("取舍原则", "body_tight"), Spacer(1, 3),
                  P("这套东西的定位是演示与技术验证，不是产线系统。凡是「接到真机才能解决」的问题——"
                    "PLC 触发、真实 QMS、多厂区台账、权限与审计——一律记为下一步，不在演示前动。"
                    "因为演示看的是判断能力与证据链，不是接入数量。", "body_tight")], bg=BG, pad=12)]

    st += [P("真要把这些限制解掉，第一步是什么", "h3"), KeepTogether(bullets([
        "<b>判据换 LLM 打分</b>：先在同一批 26 条上把新旧判据并排跑一遍，差异落在可解释范围内才换；"
        "否则换完的分数没有基线可对照，等于从头开始。",
        "<b>接真实 QMS</b>：先拿只读账号与真实字段表，把 mock 的「HTTP 200 + 业务码」换成真实响应回放，"
        "再跑 26 条看哪几条会红——先证明能读对，再谈写。",
        "<b>提响应速度</b>：先量瓶颈在模型往返还是工具查询。往返占大头就换更快的端点或压缩步数，不改架构。",
        "<b>谈稳定性</b>：对骨干用例（H1、A1、I1、W1、T2）做三轮重复，报 pass^k 而不是继续加用例——"
        "用例数不解决「同一条能不能稳定跑」。",
    ]))]
    st += [Spacer(1, 10)]
    st += [plate([P("为什么要把缺口写这么细", "body_tight"), Spacer(1, 3),
                  P("被问到一个我不会的细节，代价是这一次；说了一句「稳定 100%」以后被拿反例出来，"
                    "代价是前面所有数字都不作数。把边界先写下来，等于把追问的主动权拿回来。", "body_tight")],
                 bg=BG2, accent=ACC2, pad=12)]
    return st


def page_repro():
    st = [P("复现：三条命令、一张目录", "h2")]
    st += [P("整套流程在本机跑通，不依赖任何外部 SaaS；只有大模型走官方 API，其余（PostgreSQL、pgvector、"
             "文档检索、工单系统）都在本地。", "lead")]

    cmds = [
        P("powershell -ExecutionPolicy Bypass -File scripts\\start.ps1", "mono"),
        P("　　# 起 mock QMS（8010）与 Web 界面（8020），并打印 provider / model", "tc_m"),
        Spacer(1, 3),
        P(".\\venv\\Scripts\\python.exe eval\\run_eval.py --workers=1", "mono"),
        P("　　# 单次全量 26 条，约 5.5 分钟；报告落在 eval/report_&lt;时间&gt;_&lt;提交号&gt;.json", "tc_m"),
        Spacer(1, 3),
        P("psql -h localhost -U postgres -d fde_rag -f data\\seed\\002_data.sql", "mono"),
        P("　　# 复位十张表；复位后 work_orders 应为 1 张", "tc_m"),
    ]
    st += [Table([[cmds]], colWidths=[CW], hAlign="LEFT",
                 style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), BG),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 14),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                                   ("TOPPADDING", (0, 0), (-1, -1), 12),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 12)]))]
    st += [Spacer(1, 12)]

    rows = [
        ["路径", "内容"],
        ["app/graph", "图结构、节点、状态、模型接入（行为文件：改它才会动 code_hash）"],
        ["app/tools", "八个工具、schema、工单、手册检索"],
        ["app/web", "FastAPI 与单页界面（中文化只在展示层，不改行为）"],
        ["data/seed", "十张表的种子 SQL"],
        ["eval", "26 条用例、跑分脚本、历史报告"],
        ["mock_qms", "工单系统 mock（含「HTTP 200 + 业务码」的坑）"],
        ["docs", "36 篇过程文档，索引见 00_文档索引.md"],
    ]
    st += [tbl(rows, [84, CW - 84], pad=4.2)]
    st += [Spacer(1, 12)]

    st += [P("想继续往下读的六篇", "h3")]
    st += [KeepTogether(bullets([
        "00_文档索引.md　——　全部文档的目录、状态与作废标记",
        "20261114_01_工程清单与技术选型.md　——　最初的方案，以及被现实修正过的部分",
        "20261116_08_校验层引用式溯源与流式轨迹.md　——　溯源与轨迹是怎么落地的",
        "20261119_01_意图判据从置信改成代价.md　——　为什么阈值最后被换成分档",
        "20261119_04_结构性设计失误审查.md　——　自己审出来的结构性错误",
        "20261120_演示手卡.md　——　现场逐条脚本与翻车备份",
    ]))]
    st += [Spacer(1, 8)]
    st += [P("演示录屏：docs/demo_recordings/demo_20261120_124708.mp4（85 s，1920×1080，三幕），"
             "体积原因未入库（.gitignore 已排除）。", "small")]
    st += [Spacer(1, 10)]

    st += [P("起服务之后，先做这三项自检再演示（都是踩过坑才加的）", "h3")]
    rows = [
        ["自检项", "期望", "不符合时"],
        ["工单初态", "work_orders 只有 1 张（WO-20261105-001）", "重导 data/seed/002_data.sql，否则建单会 dedup 成旧单"],
        ["工单服务", "GET 127.0.0.1:8010/health 返回 200", "重跑 start.ps1；mock QMS 一天内掉过两次"],
        ["页面缓存", "刷新后能立刻看到改动", "根路径已加 no-cache，仍是旧页面就先清缓存"],
    ]
    st += [tbl(rows, [76, 148, CW - 224], pad=4.4)]
    st += [Spacer(1, 6)]
    st += [P("这三项都不在代码里，所以只能写进文档——演示现场 90% 的「翻车」是环境没复位，不是模型不行。", "small")]
    return st


def page_terms():
    st = [P("术语表：给不写代码的读者", "h2")]
    rows = [
        ["词", "一句话解释"],
        ["Agent", "能自己决定下一步做什么的程序：不是按固定流程走，而是根据拿到的数据选动作"],
        ["LangGraph", "把这种「可循环、可分支」的流程写成图的开源框架；状态在节点之间传递"],
        ["工具调用", "模型不直接查库，而是「提出要调用某个工具 + 参数」，由代码执行后把结果交回模型"],
        ["分流判断", "同一个现象有两种都成立的原因，Agent 的任务是先判断该往哪一边查"],
        ["引用式溯源", "结论里的每个数字都标注来自哪次调用、哪个字段，可被人逐条复核"],
        ["幂等", "同一个请求重复提交，结果与只提交一次相同；这里靠「设备+日期+根因」做键"],
        ["mock QMS", "仿造的质量管理系统，用来在本地复现真实系统的行为（包括它的坏习惯）"],
        ["pass@1", "每条用例只跑一次的成功率；它不等于稳定性，稳定性要看同一条跑多轮"],
        ["步数预算", "一次诊断最多允许调用多少次工具；到顶就必须收口，防止无限查询"],
        ["FDE", "Forward Deployed Engineer：把标准产品带到客户现场、改造适配、对交付结果负责的人"],
    ]
    st += [tbl(rows, [86, CW - 86], pad=4.6)]
    st += [Spacer(1, 12)]

    st += [P("七个常被问到的问题", "h3")]
    qa = [
        ["问", "答"],
        ["数据是真的吗？", "不是。设备型号与厂商参数名取自公开手册，可以查证；产线数据、故障分布、工单内容是构造的。首页与第 3 页都标了这一条。"],
        ["跟一个 RAG 项目有什么区别？", "RAG 的答案是「找得到的片段」，这里答案是「该往哪查的判断」：路径由证据决定，两次运行可以不同而都成立。"],
        ["为什么不接真机？", "没有真机可用。而且在接真机之前，判断逻辑与证据链应该先在可复现的数据上验证——否则数字会被现场噪声吃掉。"],
        ["26/26 能说明什么？", "只说明这一批 26 条用例、在这一版代码上单次全通（pass@1），并且带着四轴指纹。它不说明稳定性，也不说明真实产线准确率。"],
        ["最难的取舍是什么？", "判据。判据太松，模型真不会也看不出来；太严，又会把答对的判成失败。所以判据要有自己的自检和分账记录。"],
        ["为什么不用多智能体框架？", "这个场景要的是「单 Agent + 多工具 + 可恢复状态」，多智能体只会多出几条不可解释的分支；换框架的收益抵不上调试成本。"],
        ["工具为什么不用 MCP？", "现在是同进程的本地函数 + Pydantic schema，最省的写法。MCP 留作扩展：等出现跨进程、跨机器的工具时再换，不是现在。"],
    ]
    st += [tbl(qa, [132, CW - 132], pad=4.6)]
    st += [Spacer(1, 10)]
    st += [plate([P("这份文档里的每个数字、每张截图，都来自仓库内可复现的产物：报告 JSON、用例集、"
                    "单测、界面截图、录屏。凡是我判断不了的地方，已知限制那一章已经先写下来了。", "body_tight")],
                 bg=BG2, accent=ACC2, pad=12)]
    st += [Spacer(1, 10)]
    st += [P("另外：本文档只描述项目，不含个人履历——它是简历的附件，不是简历本身。"
             "如果需要看代码，仓库根目录的 README 与 00_文档索引.md 是入口。", "small")]
    return st


# ---------------------------------------------------------------- 页框
class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def _footer(self, total):
        if self._pageNumber == 1:
            return
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.6)
        self.line(ML, MB - 14, PAGE_W - MR, MB - 14)
        self.setFont("MSYH", 7.4)
        self.setFillColor(MUTE)
        self.drawString(ML, MB - 25, "视觉检测设备故障诊断 Agent · 项目介绍")
        self.drawRightString(PAGE_W - MR, MB - 25, "第 %d 页 / 共 %d 页" % (self._pageNumber, total))
        self.restoreState()


def build():
    doc = BaseDocTemplate(OUT, pagesize=A4, leftMargin=ML, rightMargin=MR, topMargin=MT, bottomMargin=MB,
                          title="视觉检测设备故障诊断 Agent · 项目介绍", author="", subject="项目介绍")
    frame = Frame(ML, MB, CW, PAGE_H - MT - MB, id="main", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame])])

    story = []
    story += cover()
    story += [PageBreak()]
    story += page_overview()
    story += [PageBreak()]
    story += page_scene()
    story += [PageBreak()]
    story += page_output()
    story += [PageBreak()]
    story += page_trace()
    story += [PageBreak()]
    story += page_arch()
    story += [PageBreak()]
    story += page_engineering()
    story += [PageBreak()]
    story += page_metrics()
    story += [PageBreak()]
    story += page_limits()
    story += [PageBreak()]
    story += page_repro()
    story += [PageBreak()]
    story += page_terms()

    doc.build(story, canvasmaker=NumberedCanvas)
    print("written", OUT, os.path.getsize(OUT))


if __name__ == "__main__":
    build()
