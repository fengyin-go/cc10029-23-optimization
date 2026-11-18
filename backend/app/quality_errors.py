"""质量指标快照子系统的领域错误与错误码。

沿用既有调用契约：旧接口继续返回 FastAPI 默认的 404 + detail 文案；新增快照接口
统一抛出 ``QualitySnapshotError``，由路由层翻译成固定错误码，前端和其他模块可以
按 ``code`` 而不是中文文案判断分支。
"""
from __future__ import annotations


class QualitySnapshotError(Exception):
    """快照领域错误基类：携带稳定错误码与 HTTP 状态码。"""

    code = "QUALITY_SNAPSHOT_ERROR"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SnapshotGeneratingError(QualitySnapshotError):
    """统计快照仍在生成（GENERATING），读端与采集校验都不允许读取。"""

    code = "QUALITY_SNAPSHOT_GENERATING"
    http_status = 409


class SnapshotNotFoundError(QualitySnapshotError):
    """指定窗口或快照编号没有已封存快照。"""

    code = "QUALITY_SNAPSHOT_NOT_FOUND"
    http_status = 404


class WindowNotFoundError(QualitySnapshotError):
    """统计窗口尚未登记（没有任何样本或生成任务）。"""

    code = "QUALITY_WINDOW_NOT_FOUND"
    http_status = 404


class SampleVersionConflictError(QualitySnapshotError):
    """原始样本与补录样本基于同一版本同时更新，发生乐观锁版本冲突。"""

    code = "QUALITY_SAMPLE_VERSION_CONFLICT"
    http_status = 409


class DatasetConflictError(QualitySnapshotError):
    """质量指标数据集并发/重复提交：只允许一个成功版本，其余按冲突拒绝。"""

    code = "QUALITY_DATASET_CONFLICT"
    http_status = 409


class DatasetNotFoundError(QualitySnapshotError):
    """读取或切换的数据集版本不存在。"""

    code = "QUALITY_DATASET_NOT_FOUND"
    http_status = 404


class ValidationBlockedError(QualitySnapshotError):
    """采集校验读取到阻断级异常窗口，校验记录被阻断项写回。"""

    code = "QUALITY_VALIDATION_BLOCKED"
    http_status = 409
