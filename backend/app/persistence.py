"""质量指标快照子系统的持久化层。

业务内存仓库 ``app.store`` 只保留模块示例数据；质量指标数据集需要在刷新、重新进入
页面后仍然一致，所以单独落到一份 JSON 文件里。真实项目这里会换成数据库事务，当前
实现只用标准库，保证克隆下来即可运行。

写入采用「同目录临时文件 + os.replace」原子替换，避免并发提交时写出半截文件。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def default_state_path() -> Path:
    """状态文件位置可由环境变量覆盖，便于测试隔离；默认放在后端工作目录下。"""
    return Path(os.environ.get("QUALITY_STATE_FILE", Path(__file__).resolve().parent.parent / "data" / "quality_state.json"))


class JsonStateStore:
    """带原子落盘能力的 JSON 状态仓库。

    状态结构由 ``QualitySnapshotService`` 维护，这里只负责读、写与首次创建，
    不在此处掺入任何业务口径。
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_state_path()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            # 损坏的状态文件不应让整个服务起不来：按空状态重建。
            return None

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".quality-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
