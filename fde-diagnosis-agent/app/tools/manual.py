"""工具 5-6：手册检索（向量）/ 文档参数冲突检测。

search_manual 查的是 device_manual_chunks（设备手册），不是项目一的 doc_chunks（IQC 文档）。
check_doc_conflict 依赖 params 字段精确比对，不做正则抽数字——否则"由 85 下调至 80"会抽错。
"""

import json
import re

from ollama import embed

from app.tools.db import connect

EMBED_MODEL = "nomic-embed-text"

# 冲突时给出参考权威顺序，但结论必须由人确认，不替用户裁定
DOC_PRIORITY = {"厂商手册": 1, "质量目标": 2, "厂内SOP": 3, "保养计划": 4, "调试记录": 5}


def _vec_literal(vec):
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def search_manual(query: str, top_k: int = 3) -> dict:
    top_k = max(1, min(int(top_k), 8))
    try:
        resp = embed(model=EMBED_MODEL, input=[query])
    except Exception as exc:
        return {"error": f"embedding 服务不可用：{exc}"}

    vlit = _vec_literal(resp["embeddings"][0])
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT source, doc_type, section, content,
                   1 - (embedding <=> %s::vector) AS score
            FROM device_manual_chunks
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (vlit, vlit, top_k))
        rows = cur.fetchall()

    if not rows:
        return {"query": query, "chunks": [], "message": "手册库为空或未命中"}

    return {
        "query": query,
        "chunks": [
            {
                "text": r["content"],
                "source_doc": r["source"],
                "doc_type": r["doc_type"],
                "section": r["section"],
                "score": round(r["score"], 4),
            }
            for r in rows
        ],
    }


def _norm_param(name):
    """参数名归一：只抹格式差，不抹语义差。

    抹空白、全角转半角、去掉尾部括号备注（"光源亮度设定（%）" → "光源亮度设定"）。
    不做同义词替换、不做拼音匹配——那一步一旦出错，会把"两个不同的参数"当成一个，
    错得比不归一化更隐蔽。
    """
    s = re.sub(r"\s+", "", str(name or ""))
    for a, b in (("（", "("), ("）", ")"), ("，", ","), ("：", ":")):
        s = s.replace(a, b)
    s = re.sub(r"\(.*?\)", "", s)
    return s


def _match_params(query, keys):
    """按"归一后相等 → 互相包含"两级找候选。

    返回 (exact, near)：
      exact  归一后完全相同（只差空格/全半角）
      near   一方是另一方的子串（"光源亮度" vs "光源亮度设定"）

    包含关系只做单向判断不够：模型既可能省略后缀（"光源亮度"），
    也可能多带限定（"产线光源亮度设定"），两边都要看。
    """
    q = _norm_param(query)
    if not q:
        return [], []
    exact = [k for k in keys if _norm_param(k) == q]
    if exact:
        return exact, []
    near = [k for k in keys
            if q in _norm_param(k) or _norm_param(k) in q]
    return [], near


def _resolve_param(query, keys):
    """决定这次该用哪个参数名去查。

    返回 (chosen, status, candidates)：

      exact       库里本来就有这个名                → 直接用
      normalized  只有一个相近名                    → 自动用它
      ambiguous   有多个相近名                      → 不猜，列出来让调用方选
      missing     一个都不像                        → 按原名查，返回可用清单

    **只在唯一候选时自动归一化。** 两个以上还自动挑一个，等于把"猜"从模型
    转移到程序——错得更隐蔽，而且调用方看不到猜的过程。
    """
    if query in keys:
        return query, "exact", []

    exact, near = _match_params(query, keys)
    if len(exact) == 1:
        return exact[0], "normalized", []
    if len(near) == 1:
        return near[0], "normalized", []
    if len(near) > 1:
        return query, "ambiguous", near
    return query, "missing", []


def _conflict_for_one(param_name, keys, cur):
    """单个参数的冲突检测。

    返回形状与单参数时代逐字段一致——旧调用方读的是 has_conflict / conflicts，
    多参数入口在只传一个名字时直接把它返回出去，对外契约不变。
    """
    chosen, status, candidates = _resolve_param(param_name, keys)

    cur.execute("""
        SELECT source, doc_type, section, params
        FROM device_manual_chunks
        WHERE params ? %s
        ORDER BY source
    """, (chosen,))
    rows = cur.fetchall()

    if not rows:
        # 参数名没命中时把可用参数列出来，让模型能改名重查，而不是直接放弃
        if status == "ambiguous":
            message = ("该参数名有几个相近的候选，请用其中一个重查"
                       f"：{'、'.join(candidates)}")
        else:
            message = "该参数名未在任何文档中出现，请改用可用参数名重查"
        return {
            "param_name": param_name,
            "has_conflict": False,
            "message": message,
            "available_params": keys,
            "suggestions": candidates if status == "ambiguous" else [],
        }

    values = [
        {
            "source": r["source"],
            "doc_type": r["doc_type"],
            "section": r["section"],
            "value": (r["params"] or {}).get(chosen),
        }
        for r in rows
    ]

    distinct = {v["value"] for v in values if v["value"] is not None}
    has_conflict = len(distinct) > 1

    authoritative = None
    if has_conflict:
        authoritative = min(values, key=lambda v: DOC_PRIORITY.get(v["doc_type"], 9))["doc_type"]

    out = {
        "param_name": chosen,
        "has_conflict": has_conflict,
        "value_count": len(distinct),
        "conflicts": values,
        "likely_authoritative": authoritative,
        "note": (
            "存在冲突，两类文档取值不同：" + "、".join(
                f"{v['doc_type']}={v['value']}" for v in values
            ) + "。取证时不要自行挑选一个继续用，须说明冲突并请人工确认以哪份为准。"
        ) if has_conflict else "各文档取值一致",
    }
    # 归一化过就明说，否则引用时会拿"光源亮度"去写 ref，而库里只有"光源亮度设定"——
    # 引用失败又被判成模型编造，其实是工具悄悄换了个名。
    if status == "normalized":
        out["normalized_from"] = param_name
        out["note"] = f"参数名「{param_name}」已按「{chosen}」归一化后查询。" + out["note"]
    return out


def check_doc_conflict(param_name: str) -> dict:
    """参数冲突检测。param_name 可用英文逗号连接多个参数，一次查完。

    为什么支持一次多值：一次诊断若涉亮度、标定周期、判定阈值，单参数版本要
    跑三轮，每轮占一格预算、算一次工具调用——而这三轮在语义上是同一个问题
    （"两份文档对得上吗"）。拆开跑把一个判断变成三次噪声，还容易被预算掐断。

    单参数时返回结构不变，多参数时额外给 by_param 与 conflict_params。
    """
    names = [p.strip() for p in str(param_name or "")
             .replace("，", ",").split(",") if p.strip()]
    if not names:
        return {"state": "bad_args", "error": "param_name 不能为空",
                "hint": "给一个参数名；多个参数用英文逗号连接，例如「光源亮度,标定周期」"}

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT jsonb_object_keys(params) AS k
            FROM device_manual_chunks WHERE params IS NOT NULL
        """)
        keys = sorted({r["k"] for r in cur.fetchall()})

        if len(names) == 1:
            return _conflict_for_one(names[0], keys, cur)

        by_param = {n: _conflict_for_one(n, keys, cur) for n in names}

    conflict_params = [n for n, r in by_param.items() if r.get("has_conflict")]
    missing = [n for n, r in by_param.items() if "message" in r]
    out = {
        "param_name": param_name,
        "has_conflict": bool(conflict_params),
        "checked": names,
        "conflict_params": conflict_params,
        "by_param": by_param,
        "note": (
            f"共查 {len(names)} 个参数，其中 {len(conflict_params)} 个存在冲突："
            + "、".join(conflict_params)
            + "。取证时不要自行挑选，须说明冲突并请人工确认以哪份为准。"
        ) if conflict_params else f"共查 {len(names)} 个参数，取值均一致",
    }
    if missing:
        out["unresolved_params"] = missing
    return out
