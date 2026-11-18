"""质量报告接口：保留旧契约，并新增质量指标快照与数据集接口。

旧接口（GET /api/quality、POST /run、GET /summary、GET /export、GET /{id}）的
路径、字段与错误码全部保留；新增接口统一返回快照领域错误码。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.quality_errors import QualitySnapshotError
from app.quality_snapshot import PERIODS, quality_snapshot_service
from app.schemas import EntryPayload
from app.services.quality import QualityService

router = APIRouter(prefix="/api/quality", tags=["质量报告"])
service = QualityService()
LIST_FIELDS = ["指标编号", "指标名称", "统计口径", "阈值下界", "阈值上界", "统计周期", "样本数量", "达标率"]
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


# ------------------------------------------------------------- 旧契约接口
@router.get("/summary")
def get_summary() -> dict[str, Any]:
    # KPI 卡片改为读取已封存快照的稳定统计，不再随机。
    return {"metrics": quality_snapshot_service.summary_metrics()}


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
                             headers={"Content-Disposition": "attachment; filename=quality.csv"})


# ------------------------------------------------------------- 采集样本
@router.get("/samples")
def list_samples(period: str | None = None, window_id: str | None = None) -> dict[str, Any]:
    """查看已落库采集样本（原始样本与补录样本）。"""
    try:
        items = quality_snapshot_service.list_samples(period=period, window_id=window_id)
    except QualitySnapshotError as error:
        _raise_domain(error)
    return {"items": items, "total": len(items)}


@router.post("/samples")
def ingest_sample(payload: EntryPayload) -> dict[str, Any]:
    """登记采集样本；迟到样本进入所属窗口，补录样本与原始样本冲突时返回版本错误。"""
    values = payload.values or {}
    try:
        sample = quality_snapshot_service.ingest_sample(
            period=str(values.get("period", "daily")),
            window_id=values.get("window_id"),
            source=str(values.get("source", "raw")),
            value=float(values.get("value", 0)),
            metric_code=values.get("metric_code"),
            expected_version=int(values["expected_version"]) if values.get("expected_version") is not None else None,
        )
    except QualitySnapshotError as error:
        _raise_domain(error)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail={"code": "QUALITY_BAD_SAMPLE", "message": str(error)})
    return {"ok": True, "message": "采集样本已落库", "item": sample}


# ------------------------------------------------------------- 统计窗口与快照
@router.get("/windows")
def list_windows(period: str | None = None) -> dict[str, Any]:
    """窗口台账：返回窗口编号、样本数量、当前快照编号与生成时间。"""
    return {"items": quality_snapshot_service.list_windows(period=period)}


@router.get("/snapshots/current")
def get_current_snapshot(period: str = "daily", window_id: str | None = None) -> dict[str, Any]:
    """读端只取已封存的当前快照；生成中/无快照返回固定错误码，页面不得补算。"""
    try:
        return quality_snapshot_service.get_current_snapshot(period=period, window_id=window_id)
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int) -> dict[str, Any]:
    try:
        return quality_snapshot_service.get_snapshot(snapshot_id)
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.post("/snapshots/generate")
def start_generation(payload: EntryPayload) -> dict[str, Any]:
    """开启窗口快照生成任务；GENERATING 期间读端与采集校验不可见。"""
    values = payload.values or {}
    period = str(values.get("period", "daily"))
    if period not in PERIODS:
        raise HTTPException(status_code=400, detail={"code": "QUALITY_BAD_PERIOD",
                                                     "message": f"统计周期仅支持 {PERIODS}"})
    try:
        return quality_snapshot_service.start_generation(period=period, window_id=values.get("window_id"))
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.post("/snapshots/seal")
def seal_generation(payload: EntryPayload) -> dict[str, Any]:
    """封存生成任务，产出带窗口编号的新不可变快照；旧快照不重写。"""
    values = payload.values or {}
    job_id = values.get("job_id")
    if not job_id:
        raise HTTPException(status_code=400, detail={"code": "QUALITY_BAD_JOB", "message": "缺少 job_id"})
    try:
        return quality_snapshot_service.seal_generation(str(job_id))
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return quality_snapshot_service.get_job(job_id)
    except QualitySnapshotError as error:
        _raise_domain(error)


# ------------------------------------------------------------- 质量指标数据集
@router.get("/datasets")
def list_datasets() -> dict[str, Any]:
    """数据集版本列表；刷新/重新进入后原记录保留，is_current 标出唯一成功版本。"""
    return {"items": quality_snapshot_service.list_datasets()}


@router.get("/datasets/current")
def get_current_dataset() -> dict[str, Any]:
    try:
        return quality_snapshot_service.get_dataset()
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.post("/datasets")
def submit_dataset(payload: EntryPayload) -> dict[str, Any]:
    """提交质量指标数据集：并发/连续重复提交只保留一个成功版本。"""
    values = payload.values or {}
    snapshot_ids = values.get("snapshot_ids") or []
    try:
        dataset = quality_snapshot_service.submit_dataset(
            snapshot_ids=[int(sid) for sid in snapshot_ids],
            remark=payload.remark,
            idempotency_key=values.get("idempotency_key"),
        )
    except QualitySnapshotError as error:
        _raise_domain(error)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail={"code": "QUALITY_BAD_DATASET", "message": str(error)})
    message = "相同数据集已存在，复用唯一成功版本" if dataset.get("deduped") else "质量指标数据集已封存"
    return {"ok": True, "message": message, "item": dataset}


@router.post("/datasets/use")
def use_dataset_version(payload: EntryPayload) -> dict[str, Any]:
    """切换旧版/新版数据集，下游采集校验随即读到不同结果。"""
    values = payload.values or {}
    try:
        dataset_id = int(values.get("dataset_id", 0))
        return quality_snapshot_service.use_dataset_version(dataset_id)
    except QualitySnapshotError as error:
        _raise_domain(error)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"code": "QUALITY_BAD_DATASET", "message": "dataset_id 非法"})


@router.get("/datasets/diff")
def diff_datasets(base: int, target: int) -> dict[str, Any]:
    """沿采集校验数据流定位校验趋势台账与数据集版本差异。"""
    try:
        return quality_snapshot_service.diff_datasets(base, target)
    except QualitySnapshotError as error:
        _raise_domain(error)


@router.get("/trend-ledger")
def list_trend_ledger() -> dict[str, Any]:
    """采集校验的校验趋势记录台账（数据集结论同步写入）。"""
    return {"items": quality_snapshot_service.list_trend_ledger()}


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量指标 {entry_id} 不存在")
    return entry
