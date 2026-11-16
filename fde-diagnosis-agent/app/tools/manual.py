"""工具 5-6：手册检索（向量）/ 文档参数冲突检测。

search_manual 查的是 device_manual_chunks（设备手册），不是项目一的 doc_chunks（IQC 文档）。
check_doc_conflict 依赖 params 字段精确比对，不做正则抽数字——否则"由 85 下调至 80"会抽错。
"""

import json

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


def check_doc_conflict(param_name: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT source, doc_type, section, params
            FROM device_manual_chunks
            WHERE params ? %s
            ORDER BY source
        """, (param_name,))
        rows = cur.fetchall()

        if not rows:
            # 参数名没命中时把可用参数列出来，让模型能改名重查，而不是直接放弃
            cur.execute("""
                SELECT DISTINCT jsonb_object_keys(params) AS k
                FROM device_manual_chunks WHERE params IS NOT NULL
            """)
            keys = sorted({r["k"] for r in cur.fetchall()})
            return {
                "param_name": param_name,
                "has_conflict": False,
                "message": "该参数名未在任何文档中出现，请改用可用参数名重查",
                "available_params": keys,
            }

        values = [
            {
                "source": r["source"],
                "doc_type": r["doc_type"],
                "section": r["section"],
                "value": (r["params"] or {}).get(param_name),
            }
            for r in rows
        ]

    distinct = {v["value"] for v in values if v["value"] is not None}
    has_conflict = len(distinct) > 1

    authoritative = None
    if has_conflict:
        authoritative = min(values, key=lambda v: DOC_PRIORITY.get(v["doc_type"], 9))["doc_type"]

    return {
        "param_name": param_name,
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
