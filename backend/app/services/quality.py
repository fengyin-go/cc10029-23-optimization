"""质量报告业务规则：指标读取全部走已封存统计快照，不再随机抽样。

旧契约保持不变：``list_results`` / ``run_analysis`` / ``get_entry`` 方法名与返回
字段沿用，原先每次读取随机生成的指标值改为读取质量指标快照数据集，保证刷新、
重新进入页面时同一快照结果一致。
"""
from __future__ import annotations

from typing import Any

from app.quality_snapshot import quality_snapshot_service
from app.store import store

MODULE = "quality"
REQUIRED_FIELDS = ["指标编号", "指标名称", "统计口径"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class QualityService:
    def list_results(self) -> list[dict[str, Any]]:
        """读取当前数据集/各窗口当前快照的指标记录，确定性结果、无随机抽样。"""
        return quality_snapshot_service.current_snapshot_rows()

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """运行分析只消费已封存快照；页面不允许自行补算缺失样本。"""
        return quality_snapshot_service.run_analysis(params or {})

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)
