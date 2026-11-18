"""采集校验业务规则：待办流只读取已封存质量指标数据集，阻断项写回校验记录。

旧契约（``list_results`` / ``run_analysis`` / ``get_entry``）保留，原先随机生成的
指标值改为读取质量报告封存的当前数据集；正在生成的指标不允许读取。
"""
from __future__ import annotations

from typing import Any

from app.quality_snapshot import quality_snapshot_service
from app.store import store

MODULE = "validate"
REQUIRED_FIELDS = ["规则编号", "校验字段", "期望格式"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class ValidateService:
    def list_results(self) -> list[dict[str, Any]]:
        """校验看板数据来自当前已封存数据集；无数据集时返回空列表，绝不自行补算。"""
        rows = store.rows(MODULE)
        quality_rows = quality_snapshot_service.current_snapshot_rows()
        # 以质量快照中的窗口-指标记录为准，与校验规则按指标编号对齐；无快照即空。
        results: list[dict[str, Any]] = []
        for index, qrow in enumerate(quality_rows, start=1):
            d = dict(qrow)
            d["id"] = index
            matched = next((r for r in rows if r.get("规则编号")), None)
            if matched:
                d["规则编号"] = matched.get("规则编号")
            results.append(d)
        return results

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """运行分析复用质量报告的确定性快照聚合，保留原返回字段。"""
        return quality_snapshot_service.run_analysis(params or {})

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ------------------------------------------------ 待办流：异常窗口与阻断写回
    def fetch_todos(self) -> dict[str, Any]:
        return quality_snapshot_service.list_validation_todos()

    def writeback(self, todo_id: int, rule_id: int | None = None) -> dict[str, Any]:
        return quality_snapshot_service.writeback_validation(todo_id, rule_id)

    def list_writebacks(self) -> list[dict[str, Any]]:
        return quality_snapshot_service.list_writebacks()

    def trend_ledger(self) -> list[dict[str, Any]]:
        return quality_snapshot_service.list_trend_ledger()

    def diff_datasets(self, base: int, target: int) -> dict[str, Any]:
        return quality_snapshot_service.diff_datasets(base, target)
