"""工具包出口：统一导出 schema 与实现映射。"""

from app.tools.diagnosis import (
    query_calibration,
    query_defect_log,
    query_device_profile,
    query_light_usage,
)
from app.tools.manual import check_doc_conflict, search_manual
from app.tools.schemas import TOOL_SCHEMAS
from app.tools.workorder import create_work_order, query_work_order

TOOL_IMPLS = {
    "query_device_profile": query_device_profile,
    "query_defect_log": query_defect_log,
    "query_calibration": query_calibration,
    "query_light_usage": query_light_usage,
    "search_manual": search_manual,
    "check_doc_conflict": check_doc_conflict,
    "query_work_order": query_work_order,
    "create_work_order": create_work_order,
}

__all__ = ["TOOL_SCHEMAS", "TOOL_IMPLS"]
