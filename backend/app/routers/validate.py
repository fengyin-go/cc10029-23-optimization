"""采集校验接口：保留旧契约，新增质量指标待办流、阻断写回与趋势台账接口。

采集校验只允许读取质量报告已封存的指标数据集；正在生成的指标由快照服务抛出
``QUALITY_SNAPSHOT_GENERATING``，本层翻译成 409 固定错误码。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.quality_errors import QualitySnapshotError
from app.quality_snapshot import quality_snapshot_service
from app.schemas import EntryPayload
from app.services.validate import ValidateService

router = APIRouter(prefix="/api/validate", tags=["采集校验"])
service = ValidateService()
LIST_FIELDS = ["规则编号", "校验字段", "期望格式", "空值阈值", "重复判定", "异常策略", "阻断级别", "校验批次"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _raise_domain(error: QualitySnapshotError) -> None:
    raise HTTPException(
        status_code=error.http_status,
        detail={"code": error.code, "message": error.message, "details": error.details},
    )


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    # 汇总卡片来自已封存数据集与待办流，不再随机。
    try:
        todo_payload = quality_snapshot_service.list_validation_todos()
    except QualitySnapshotError as error:
        _raise_domain(error)
    todos = todo_payload["todos"]
    pending = sum(1 for todo in todos if todo["status"] == "PENDING")
    ledger = quality_snapshot_service.list_trend_ledger()
    latest_rate = ledger[-1]["average_pass_rate"] if ledger else 0
    trend = round((latest_rate - ledger[-2]["average_pass_rate"]) * 100, 1) if len(ledger) >= 2 else 0
    return {"metrics": [
        {"label": "覆盖数据源", "value": len({t["window_id"] for t in todos}), "trend": 0},
        {"label": "最新指标值", "value": latest_rate, "trend": trend},
        {"label": "异常记录", "value": pending, "trend": 0},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=validate.csv"})


# ----------------------------------------------- 质量指标待办流与阻断项写回
@router.get("/quality-todos")
def quality_todos() -> dict[str, Any]:
    """待办流：从当前已封存质量指标数据集获取异常窗口，不读取生成中的指标。"""
    try:
        return service.fetch_todos()
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.post("/quality-todos/{todo_id}/writeback")
def writeback(todo_id: int, payload: EntryPayload | None = None) -> dict[str, Any]:
    """把阻断项写回校验记录；校验规则上同步记录阻断项与关联快照编号。"""
    rule_id = None
    if payload is not None and payload.values:
        try:
            rule_id = int(payload.values["rule_id"]) if payload.values.get("rule_id") is not None else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"code": "QUALITY_BAD_RULE", "message": "rule_id 非法"})
    try:
        record = service.writeback(todo_id, rule_id)
    except QualitySnapshotError as error:
        _raise_domain(error)
    return {"ok": True, "message": "阻断项已写回校验记录", "item": record}


@router.get("/writebacks")
def list_writebacks() -> dict[str, Any]:
    return {"items": service.list_writebacks()}


@router.get("/trend-ledger")
def trend_ledger() -> dict[str, Any]:
    """校验趋势记录台账：数据集结论同步后在此可查。"""
    return {"items": service.trend_ledger()}


@router.get("/dataset-diff")
def dataset_diff(base: int, target: int) -> dict[str, Any]:
    """顺着采集校验数据流定位校验趋势记录与质量指标数据集的版本差异。"""
    try:
        return service.diff_datasets(base, target)
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"校验规则 {entry_id} 不存在")
    return entry
