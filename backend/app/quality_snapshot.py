"""质量指标快照子系统：从已落库采集样本按指标口径聚合，封存为不可变统计快照。

设计要点（对应验收口径）：

1. 读端只能读取 ``SEALED`` 已封存快照；``GENERATING`` 任务对读端不可见，
   采集校验也不允许读取正在生成的指标。
2. 每个统计窗口登记统计周期、窗口编号、样本数量与生成时间；页面刷新、重新进入
   读到的是同一份快照，结果不再漂移。
3. 迟到采集记录进入所属窗口后，重新封存只会生成该窗口的新快照（窗口快照编号
   自增），已封存快照不可原地重写，窗口只保留一个 ``current_snapshot_id``。
4. 原始样本与补录样本基于同一窗口版本同时更新时，用乐观锁抛出版本冲突，
   页面仍只能展示带窗口编号的当前快照。
5. 质量指标数据集是快照的持久化提交物：并发/连续重复提交只保留一个成功版本，
   通过幂等键与内容指纹去重；采集校验固定读取一个版本。
6. 数据集结论同步到采集校验的校验趋势记录台账，并可沿数据流定位台账与数据集
   的差异；旧版/新版数据集会得到不同的下游校验结果。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta
from typing import Any

from app.persistence import JsonStateStore
from app.quality_errors import (
    DatasetConflictError,
    DatasetNotFoundError,
    SampleVersionConflictError,
    SnapshotGeneratingError,
    SnapshotNotFoundError,
    WindowNotFoundError,
)
from app.seed import SEED_ROWS
from app.store import store

MODULE = "quality"
VALIDATE_MODULE = "validate"
PERIODS = ("daily", "weekly", "monthly")
PERIOD_LABELS = {"daily": "按日", "weekly": "按周", "monthly": "按月"}
JOB_GENERATING = "GENERATING"
SNAPSHOT_SEALED = "SEALED"
TODO_PENDING = "PENDING"
TODO_DONE = "DONE"

# 阈值字段可能是样例中文文案，无法解析时回退到该默认达标区间。
DEFAULT_LOWER = 0.6
DEFAULT_UPPER = 1.0


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def window_id_of(period: str, now: datetime | None = None) -> str:
    """按统计周期计算当前窗口编号（窗口的稳定业务标识）。"""
    now = now or datetime.now()
    if period == "daily":
        return f"D-{now.strftime('%Y%m%d')}"
    if period == "weekly":
        iso_year, iso_week, _ = now.isocalendar()
        return f"W-{iso_year}{iso_week:02d}"
    if period == "monthly":
        return f"M-{now.strftime('%Y%m')}"
    raise ValueError(f"不支持的统计周期：{period}")


def shift_window_id(period: str, window_id: str, steps: int) -> str:
    """把窗口编号平移 steps 个周期，用于登记历史窗口与迟到样本。"""
    if period == "daily":
        base = datetime.strptime(window_id[2:], "%Y%m%d")
        return window_id_of(period, base + timedelta(days=steps))
    if period == "monthly":
        base = datetime.strptime(window_id[2:], "%Y%m")
        month_index = base.year * 12 + (base.month - 1) + steps
        return f"M-{month_index // 12:04d}{month_index % 12 + 1:02d}"
    if period == "weekly":
        year, week = int(window_id[2:6]), int(window_id[6:8])
        # 借助 ISO 日历：取该周周一再平移整周。
        monday = datetime.fromisocalendar(year, week, 1) + timedelta(weeks=steps)
        iso_year, iso_week, _ = monday.isocalendar()
        return f"W-{iso_year}{iso_week:02d}"
    raise ValueError(f"不支持的统计周期：{period}")


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _metric_thresholds(row: dict[str, Any]) -> tuple[float, float]:
    lower = _to_float(row.get("阈值下界"), DEFAULT_LOWER)
    upper = _to_float(row.get("阈值上界"), DEFAULT_UPPER)
    if lower > upper:
        lower, upper = upper, lower
    return lower, upper


def _metric_baseline(row: dict[str, Any]) -> float:
    """对比基准取阈值下界与 1.0 的中点，保证同一指标口径基准稳定。"""
    lower, upper = _metric_thresholds(row)
    return round((lower + min(upper, 1.0)) / 2, 4)


class QualitySnapshotService:
    """质量指标快照与数据集的领域服务；进程内单例，线程安全。"""

    def __init__(self, state_store: JsonStateStore | None = None) -> None:
        self._store = state_store or JsonStateStore()
        self._lock = threading.RLock()
        self._state = self._load_or_init()

    # ------------------------------------------------------------------ 状态
    def _blank_state(self) -> dict[str, Any]:
        return {
            "samples": [],
            "windows": {},
            "snapshots": {},
            "jobs": {},
            "datasets": [],
            "current_dataset_id": None,
            "todos": [],
            "trend_ledger": [],
            "writebacks": [],
            "seq": {"sample": 0, "snapshot": 0, "job": 0, "dataset": 0, "todo": 0, "ledger": 0, "writeback": 0},
        }

    def _load_or_init(self) -> dict[str, Any]:
        state = self._store.load()
        if state is None:
            state = self._blank_state()
            self._seed_samples(state)
            self._seed_initial_snapshots(state)
            self._persist(state)
        else:
            # JSON 对象键会被转成字符串，快照以整数 id 为键，加载后统一转回 int。
            state["snapshots"] = {int(key): value for key, value in state.get("snapshots", {}).items()}
        return state

    def _persist(self, state: dict[str, Any] | None = None) -> None:
        self._store.save(state or self._state)

    def _next_id(self, kind: str) -> int:
        seq = self._state["seq"]
        seq[kind] = int(seq.get(kind, 0)) + 1
        return seq[kind]

    # ------------------------------------------------------------ 样本落库
    def _seed_samples(self, state: dict[str, Any]) -> None:
        """首次启动时把确定性的采集样本落库，后续重启直接复用，不再重抽。

        样本值由固定种子的 LCG 生成，同一份状态文件永远得到同一批样本，
        聚合结果因此稳定可复现。
        """
        seed = 20261118
        period_windows = {period: window_id_of(period) for period in PERIODS}

        def lcg() -> float:
            nonlocal seed
            # 数值配方常量（Numerical Recipes），纯标准库即可稳定复现。
            seed = (1664525 * seed + 1013904223) % 2**32
            return seed / 2**32

        sample_seq = 0
        for period in PERIODS:
            current = period_windows[period]
            # 当前窗口与上一窗口都铺样本，便于切换周期、比较快照编号。
            for step in (0, -1):
                wid = current if step == 0 else shift_window_id(period, current, -1)
                window = self._ensure_window(state, period, wid)
                count = 24 if step == 0 else 18
                for index in range(count):
                    sample_seq += 1
                    value = round(0.55 + lcg() * 0.44, 4)  # 0.55 ~ 0.99
                    state["samples"].append({
                        "id": sample_seq,
                        "window_id": wid,
                        "period": period,
                        "source": "raw" if index % 3 else "supplement",
                        "value": value,
                        "metric_code": None,
                        "ingested_at": _now_iso(),
                    })
                # 样本版本即窗口已落库样本的更新次数，种子样本同样计入，
                # 否则基于版本 0 的乐观锁会把已落库窗口误判成无更新。
                window["sample_version"] = count
        state["seq"]["sample"] = sample_seq

    @staticmethod
    def _new_window(wid: str, period: str) -> dict[str, Any]:
        return {
            "window_id": wid,
            "period": period,
            "sample_version": 0,
            "snapshot_seq": 0,
            "snapshot_ids": [],
            "current_snapshot_id": None,
            "generating_job_id": None,
        }

    def _ensure_window(self, state: dict[str, Any], period: str, wid: str) -> dict[str, Any]:
        window = state["windows"].get(wid)
        if window is None:
            window = self._new_window(wid, period)
            state["windows"][wid] = window
        return window

    def ingest_sample(
        self,
        *,
        period: str,
        value: float,
        source: str = "raw",
        window_id: str | None = None,
        metric_code: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """登记一条采集样本（含迟到/补录）；带期望版本时做乐观锁校验。"""
        if period not in PERIODS:
            raise ValueError(f"不支持的统计周期：{period}")
        if source not in ("raw", "supplement"):
            raise ValueError("样本来源只允许 raw（原始样本）或 supplement（补录样本）")
        wid = window_id or window_id_of(period)
        with self._lock:
            window = self._ensure_window(self._state, period, wid)
            if expected_version is not None and int(expected_version) != int(window["sample_version"]):
                raise SampleVersionConflictError(
                    "原始样本与补录样本同时更新导致版本冲突，请基于最新窗口版本重试",
                    details={
                        "window_id": wid,
                        "expected_version": int(expected_version),
                        "current_version": int(window["sample_version"]),
                    },
                )
            sample = {
                "id": self._next_id("sample"),
                "window_id": wid,
                "period": period,
                "source": source,
                "value": float(value),
                "metric_code": metric_code,
                "ingested_at": _now_iso(),
            }
            self._state["samples"].append(sample)
            window["sample_version"] += 1
            self._persist()
            return dict(sample, window_sample_version=window["sample_version"])

    def list_samples(self, period: str | None = None, window_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._state["samples"]
            if period:
                rows = [row for row in rows if row["period"] == period]
            if window_id:
                rows = [row for row in rows if row["window_id"] == window_id]
            return [dict(row) for row in rows]

    # ------------------------------------------------------------ 快照生成
    def _window_samples(self, state: dict[str, Any], wid: str) -> list[dict[str, Any]]:
        return [row for row in state["samples"] if row["window_id"] == wid]

    def _aggregate(self, wid: str, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按质量指标口径（指标编号）聚合窗口样本，输出不可变指标记录。"""
        metric_rows = store.rows(MODULE)
        records: list[dict[str, Any]] = []
        for row in metric_rows:
            code = str(row.get("指标编号") or "")
            lower, upper = _metric_thresholds(row)
            scoped = [s for s in samples if not s.get("metric_code") or s["metric_code"] == code]
            total = len(scoped)
            passed = sum(1 for s in scoped if lower <= float(s["value"]) <= upper)
            rate = round(passed / total, 4) if total else 0.0
            baseline = _metric_baseline(row)
            records.append({
                "指标编号": code,
                "指标名称": row.get("指标名称"),
                "统计口径": row.get("统计口径"),
                "阈值下界": lower,
                "阈值上界": upper,
                "统计周期": row.get("统计周期"),
                "样本数量": total,
                "达标率": rate,
                "指标值": rate,
                "对比基准": baseline,
                "差异": round(rate - baseline, 4),
                "达标": bool(total and lower <= rate <= upper),
            })
        return records

    def start_generation(self, period: str, window_id: str | None = None) -> dict[str, Any]:
        """开启（或复用）窗口的生成任务；任务期间读端拿不到该窗口快照。"""
        if period not in PERIODS:
            raise ValueError(f"不支持的统计周期：{period}")
        wid = window_id or window_id_of(period)
        with self._lock:
            window = self._ensure_window(self._state, period, wid)
            existing_id = window.get("generating_job_id")
            if existing_id and existing_id in self._state["jobs"]:
                return dict(self._state["jobs"][existing_id])
            job = {
                "job_id": f"JOB-{wid}-{self._next_id('job')}",
                "window_id": wid,
                "period": period,
                "status": JOB_GENERATING,
                "started_at": _now_iso(),
                "sealed_snapshot_id": None,
            }
            self._state["jobs"][job["job_id"]] = job
            window["generating_job_id"] = job["job_id"]
            self._persist()
            return dict(job)

    def seal_generation(self, job_id: str) -> dict[str, Any]:
        """封存生成任务：聚合当前窗口全部样本，产出不可变新快照。

        已封存快照永不原地改写；重复封存同一窗口只会得到编号更大的新快照，
        并把窗口当前指针切到新快照。
        """
        with self._lock:
            job = self._state["jobs"].get(job_id)
            if job is None:
                raise SnapshotNotFoundError(f"生成任务 {job_id} 不存在")
            if job["status"] != JOB_GENERATING:
                raise SnapshotGeneratingError(
                    f"生成任务 {job_id} 已封存，不能重复封存",
                    details={"job_id": job_id, "status": job["status"]},
                )
            wid = job["window_id"]
            window = self._state["windows"][wid]
            samples = self._window_samples(self._state, wid)
            records = self._aggregate(wid, samples)
            snapshot_id = self._next_id("snapshot")
            window["snapshot_seq"] += 1
            snapshot = {
                "snapshot_id": snapshot_id,
                "snapshot_no": f"SNAP-{wid}-{window['snapshot_seq']:03d}",
                "window_id": wid,
                "period": window["period"],
                "window_seq": window["snapshot_seq"],
                "status": SNAPSHOT_SEALED,
                "sample_count": len(samples),
                "sample_version": int(window["sample_version"]),
                "generated_at": _now_iso(),
                "metric_records": records,
            }
            self._state["snapshots"][snapshot_id] = snapshot
            window["snapshot_ids"].append(snapshot_id)
            window["current_snapshot_id"] = snapshot_id
            window["generating_job_id"] = None
            job["status"] = SNAPSHOT_SEALED
            job["sealed_snapshot_id"] = snapshot_id
            job["finished_at"] = snapshot["generated_at"]
            self._persist()
            return dict(snapshot)

    def _seed_initial_snapshots(self, state: dict[str, Any]) -> None:
        """给每个周期的两个种子窗口直接封存首版快照，保证首屏即有稳定数据。"""
        for wid, window in list(state["windows"].items()):
            samples = [row for row in state["samples"] if row["window_id"] == wid]
            records = self._aggregate(wid, samples)
            snapshot_id = len(state["snapshots"]) + 1
            window["snapshot_seq"] = 1
            snapshot = {
                "snapshot_id": snapshot_id,
                "snapshot_no": f"SNAP-{wid}-001",
                "window_id": wid,
                "period": window["period"],
                "window_seq": 1,
                "status": SNAPSHOT_SEALED,
                "sample_count": len(samples),
                "sample_version": int(window["sample_version"]),
                "generated_at": _now_iso(),
                "metric_records": records,
            }
            state["snapshots"][snapshot_id] = snapshot
            window["snapshot_ids"] = [snapshot_id]
            window["current_snapshot_id"] = snapshot_id
        state["seq"]["snapshot"] = len(state["snapshots"])

    # ------------------------------------------------------------ 快照读取
    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._state["jobs"].get(job_id)
            if job is None:
                raise SnapshotNotFoundError(f"生成任务 {job_id} 不存在")
            return dict(job)

    def list_windows(self, period: str | None = None) -> list[dict[str, Any]]:
        """窗口台账：只暴露已封存快照的窗口，正在生成的窗口不会被读端消费。"""
        with self._lock:
            result = []
            for window in self._state["windows"].values():
                if period and window["period"] != period:
                    continue
                current_id = window.get("current_snapshot_id")
                snapshot = self._state["snapshots"].get(current_id) if current_id else None
                if window.get("generating_job_id"):
                    # 生成中优先暴露 GENERATING：旧快照仍在但读端必须感知指标正在重算。
                    result.append({
                        "window_id": window["window_id"],
                        "period": window["period"],
                        "sample_count": len(self._window_samples(self._state, window["window_id"])),
                        "sample_version": window["sample_version"],
                        "status": JOB_GENERATING,
                        "generating_job_id": window["generating_job_id"],
                        "current_snapshot": self._snapshot_view(snapshot) if snapshot else None,
                    })
                    continue
                if snapshot is None:
                    # 读端只能使用已封存快照：无快照窗口仅提示生成中/无数据。
                    result.append({
                        "window_id": window["window_id"],
                        "period": window["period"],
                        "sample_count": len(self._window_samples(self._state, window["window_id"])),
                        "sample_version": window["sample_version"],
                        "status": "NO_SNAPSHOT",
                        "generating_job_id": None,
                        "current_snapshot": None,
                    })
                    continue
                result.append({
                    "window_id": window["window_id"],
                    "period": window["period"],
                    "sample_count": snapshot["sample_count"],
                    "sample_version": window["sample_version"],
                    "status": SNAPSHOT_SEALED,
                    "generating_job_id": None,
                    "current_snapshot": self._snapshot_view(snapshot),
                })
            return sorted(result, key=lambda item: item["window_id"], reverse=True)

    def _snapshot_view(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in snapshot.items() if k != "metric_records"}
        view["period_label"] = PERIOD_LABELS.get(snapshot["period"], snapshot["period"])
        return view

    def get_current_snapshot(self, period: str, window_id: str | None = None) -> dict[str, Any]:
        """读取窗口当前已封存快照；正在生成或没有快照时明确报错，页面不得补算。"""
        wid = window_id or window_id_of(period)
        with self._lock:
            window = self._state["windows"].get(wid)
            if window is None:
                raise WindowNotFoundError(f"统计窗口 {wid} 尚未登记", details={"window_id": wid})
            if window.get("generating_job_id"):
                raise SnapshotGeneratingError(
                    f"窗口 {wid} 的质量指标正在生成，读端暂不可用",
                    details={"window_id": wid, "job_id": window["generating_job_id"]},
                )
            snapshot_id = window.get("current_snapshot_id")
            snapshot = self._state["snapshots"].get(snapshot_id) if snapshot_id else None
            if snapshot is None:
                raise SnapshotNotFoundError(
                    f"窗口 {wid} 没有已封存快照", details={"window_id": wid}
                )
            return dict(snapshot)

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        with self._lock:
            snapshot = self._state["snapshots"].get(int(snapshot_id))
            if snapshot is None:
                raise SnapshotNotFoundError(f"快照 {snapshot_id} 不存在")
            return dict(snapshot)

    # ------------------------------------------------------------ 数据集提交
    @staticmethod
    def _fingerprint(entries: list[dict[str, Any]]) -> str:
        """按窗口编号+快照编号计算内容指纹，相同提交物指纹一致。"""
        parts = [f"{e['window_id']}:{e['snapshot_no']}" for e in entries]
        raw = "|".join(sorted(parts))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def submit_dataset(
        self,
        *,
        snapshot_ids: list[int],
        remark: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """提交质量指标数据集。

        并发两份、连续两次相同提交都只会保留一个成功版本：
        - 幂等键命中：直接返回首次成功版本（deduped=true）；
        - 内容指纹命中：返回 409 冲突，并指明已存在的成功版本；
        - 否则封存新版本并成为当前版本，结论写入校验趋势台账。
        """
        if not snapshot_ids:
            raise ValueError("至少选择一个已封存快照才能提交数据集")
        with self._lock:
            if idempotency_key:
                for dataset in self._state["datasets"]:
                    if dataset.get("idempotency_key") == idempotency_key:
                        return dict(dataset, deduped=True)
            entries = []
            for sid in dict.fromkeys(int(x) for x in snapshot_ids):
                snapshot = self._state["snapshots"].get(sid)
                if snapshot is None:
                    raise SnapshotNotFoundError(f"快照 {sid} 不存在，不能纳入数据集")
                if snapshot["status"] != SNAPSHOT_SEALED:
                    raise SnapshotGeneratingError(
                        f"快照 {sid} 尚未封存，不能纳入数据集", details={"snapshot_id": sid}
                    )
                entries.append({
                    "window_id": snapshot["window_id"],
                    "period": snapshot["period"],
                    "snapshot_id": snapshot["snapshot_id"],
                    "snapshot_no": snapshot["snapshot_no"],
                    "sample_count": snapshot["sample_count"],
                    "generated_at": snapshot["generated_at"],
                    "metric_records": [dict(r) for r in snapshot["metric_records"]],
                })
            fingerprint = self._fingerprint(entries)
            for dataset in self._state["datasets"]:
                if dataset["fingerprint"] == fingerprint:
                    raise DatasetConflictError(
                        "相同内容的质量指标数据集已提交，只保留一个成功版本",
                        details={"existing_dataset_id": dataset["dataset_id"], "fingerprint": fingerprint},
                    )
            dataset_id = self._next_id("dataset")
            version = f"QDS-V{dataset_id:03d}"
            dataset = {
                "dataset_id": dataset_id,
                "version": version,
                "fingerprint": fingerprint,
                "idempotency_key": idempotency_key,
                "remark": remark or "",
                "status": "SEALED",
                "created_at": _now_iso(),
                "entry_count": len(entries),
                "entries": entries,
            }
            self._state["datasets"].append(dataset)
            self._state["current_dataset_id"] = dataset_id
            self._append_trend_ledger(dataset, reason="DATASET_SUBMITTED")
            self._persist()
            return dict(dataset, deduped=False)

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._dataset_summary(d) for d in self._state["datasets"]]

    def _dataset_summary(self, dataset: dict[str, Any]) -> dict[str, Any]:
        return {
            "dataset_id": dataset["dataset_id"],
            "version": dataset["version"],
            "fingerprint": dataset["fingerprint"],
            "status": dataset["status"],
            "remark": dataset.get("remark", ""),
            "created_at": dataset["created_at"],
            "entry_count": dataset["entry_count"],
            "is_current": self._state["current_dataset_id"] == dataset["dataset_id"],
            "idempotency_key": dataset.get("idempotency_key"),
        }

    def get_dataset(self, dataset_id: int | None = None) -> dict[str, Any]:
        with self._lock:
            target_id = int(dataset_id) if dataset_id is not None else self._state["current_dataset_id"]
            dataset = next((d for d in self._state["datasets"] if d["dataset_id"] == target_id), None)
            if dataset is None:
                raise DatasetNotFoundError(
                    "质量指标数据集不存在" if dataset_id is not None else "尚无已提交的质量指标数据集"
                )
            result = dict(dataset)
            result["is_current"] = self._state["current_dataset_id"] == dataset["dataset_id"]
            return result

    def use_dataset_version(self, dataset_id: int) -> dict[str, Any]:
        """质量报告切换旧版/新版数据集；下游采集校验随即读到不同结果。"""
        with self._lock:
            dataset = next((d for d in self._state["datasets"] if d["dataset_id"] == int(dataset_id)), None)
            if dataset is None:
                raise DatasetNotFoundError(f"数据集版本 {dataset_id} 不存在")
            self._state["current_dataset_id"] = dataset["dataset_id"]
            self._append_trend_ledger(dataset, reason="DATASET_ACTIVATED")
            self._persist()
            return self._dataset_summary(dataset)

    # ------------------------------------------------------------ 采集校验
    def _abnormal_entries(self, dataset: dict[str, Any]) -> list[dict[str, Any]]:
        """沿采集校验数据流：把数据集内未达指标阈值的窗口判为异常窗口。"""
        abnormal = []
        for entry in dataset["entries"]:
            bad_records = [r for r in entry["metric_records"] if not r["达标"]]
            if bad_records:
                abnormal.append({
                    "window_id": entry["window_id"],
                    "period": entry["period"],
                    "snapshot_no": entry["snapshot_no"],
                    "sample_count": entry["sample_count"],
                    "generated_at": entry["generated_at"],
                    "abnormal_metrics": [
                        {"指标编号": r["指标编号"], "达标率": r["达标率"],
                         "阈值下界": r["阈值下界"], "阈值上界": r["阈值上界"]}
                        for r in bad_records
                    ],
                })
        return abnormal

    def list_validation_todos(self) -> dict[str, Any]:
        """采集校验待办流：只从当前已封存数据集读取异常窗口，不读生成中的指标。

        待办与当前数据集版本严格对齐：切换旧版/新版后，待办流只呈现当前版本的
        异常窗口；历史待办与写回记录仍保留在写回台账里可追溯，不再混进当前待办。
        """
        with self._lock:
            current_id = self._state["current_dataset_id"]
            if current_id is None:
                return {"dataset_version": None, "todos": []}
            dataset = self.get_dataset(current_id)
            abnormal = self._abnormal_entries(dataset)
            abnormal_windows = {item["window_id"] for item in abnormal}
            for item in abnormal:
                # 同一窗口在当前数据集下只保留一条待办：已有（含已写回）则沿用，
                # 不重复生成；新出现的异常窗口才新建。
                existing = next(
                    (t for t in self._state["todos"]
                     if t["window_id"] == item["window_id"] and t["dataset_id"] == dataset["dataset_id"]),
                    None,
                )
                if existing is not None:
                    continue
                todo = {
                    "todo_id": self._next_id("todo"),
                    "window_id": item["window_id"],
                    "period": item["period"],
                    "snapshot_no": item["snapshot_no"],
                    "dataset_id": dataset["dataset_id"],
                    "dataset_version": dataset["version"],
                    "level": "阻断",
                    "status": TODO_PENDING,
                    "detail": item,
                    "created_at": _now_iso(),
                }
                self._state["todos"].append(todo)
            self._persist()
            # 读端只看当前数据集版本、且窗口仍异常的待办，避免旧版本残留串到下游。
            active = [
                dict(t) for t in self._state["todos"]
                if t["dataset_id"] == dataset["dataset_id"] and t["window_id"] in abnormal_windows
            ]
            return {
                "dataset_version": dataset["version"],
                "dataset_id": dataset["dataset_id"],
                "todos": active,
            }

    def writeback_validation(self, todo_id: int, rule_id: int | None = None) -> dict[str, Any]:
        """把阻断项写回采集校验记录，并登记一条写回台账。"""
        with self._lock:
            todo = next((t for t in self._state["todos"] if t["todo_id"] == int(todo_id)), None)
            if todo is None:
                raise WindowNotFoundError(f"校验待办 {todo_id} 不存在")
            target_rule = None
            if rule_id is not None:
                target_rule = store.find(VALIDATE_MODULE, int(rule_id))
                if target_rule is None:
                    raise SnapshotNotFoundError(f"校验规则 {rule_id} 不存在")
            if target_rule is None:
                rules = [r for r in store.rows(VALIDATE_MODULE) if r.get("阻断级别")]
                target_rule = rules[0] if rules else (store.rows(VALIDATE_MODULE)[0] if store.rows(VALIDATE_MODULE) else None)
            writeback = {
                "writeback_id": self._next_id("writeback"),
                "todo_id": todo["todo_id"],
                "window_id": todo["window_id"],
                "dataset_version": todo["dataset_version"],
                "rule_id": target_rule.get("id") if target_rule else None,
                "rule_code": target_rule.get("规则编号") if target_rule else None,
                "blocking_items": todo["detail"]["abnormal_metrics"],
                "written_at": _now_iso(),
            }
            if target_rule is not None:
                target_rule["abnormal"] = True
                target_rule["pending"] = True
                target_rule["质量阻断项"] = [
                    f"{m['指标编号']} 达标率{m['达标率']} 超出阈值[{m['阈值下界']},{m['阈值上界']}]"
                    for m in todo["detail"]["abnormal_metrics"]
                ]
                target_rule["关联质量快照"] = todo["snapshot_no"]
            todo["status"] = TODO_DONE
            todo["finished_at"] = _now_iso()
            self._state["writebacks"].append(writeback)
            self._persist()
            return dict(writeback)

    def list_writebacks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(w) for w in self._state["writebacks"]]

    # ------------------------------------------------------------ 趋势台账
    def _append_trend_ledger(self, dataset: dict[str, Any], *, reason: str) -> None:
        abnormal = self._abnormal_entries(dataset)
        total_records = sum(len(e["metric_records"]) for e in dataset["entries"])
        avg_rate = (
            round(sum(r["达标率"] for e in dataset["entries"] for r in e["metric_records"]) / total_records, 4)
            if total_records else 0.0
        )
        ledger = {
            "ledger_id": self._next_id("ledger"),
            "dataset_id": dataset["dataset_id"],
            "dataset_version": dataset["version"],
            "reason": reason,
            "window_count": dataset["entry_count"],
            "abnormal_window_count": len(abnormal),
            "average_pass_rate": avg_rate,
            "recorded_at": _now_iso(),
        }
        self._state["trend_ledger"].append(ledger)

    def list_trend_ledger(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._state["trend_ledger"]]

    def diff_datasets(self, base_dataset_id: int, target_dataset_id: int) -> dict[str, Any]:
        """顺着采集校验数据流定位校验趋势记录与质量指标数据集的差异。"""
        with self._lock:
            base = self.get_dataset(base_dataset_id)
            target = self.get_dataset(target_dataset_id)
            base_map = {e["window_id"]: e for e in base["entries"]}
            target_map = {e["window_id"]: e for e in target["entries"]}
            window_diffs = []
            for wid in sorted(set(base_map) | set(target_map)):
                left, right = base_map.get(wid), target_map.get(wid)
                if left is None or right is None:
                    window_diffs.append({"window_id": wid, "change_type": "ADDED" if right else "REMOVED",
                                         "snapshot_base": left and left["snapshot_no"],
                                         "snapshot_target": right and right["snapshot_no"], "metrics": []})
                    continue
                metric_diffs = []
                left_records = {r["指标编号"]: r for r in left["metric_records"]}
                right_records = {r["指标编号"]: r for r in right["metric_records"]}
                for code in sorted(set(left_records) | set(right_records)):
                    lr, rr = left_records.get(code), right_records.get(code)
                    if lr is None or rr is None or lr["达标率"] != rr["达标率"] or lr["达标"] != rr["达标"]:
                        metric_diffs.append({
                            "指标编号": code,
                            "base_snapshot": left["snapshot_no"],
                            "target_snapshot": right["snapshot_no"],
                            "base_pass_rate": lr and lr["达标率"],
                            "target_pass_rate": rr and rr["达标率"],
                            "base_qualified": lr and lr["达标"],
                            "target_qualified": rr and rr["达标"],
                        })
                if metric_diffs or left["snapshot_no"] != right["snapshot_no"]:
                    window_diffs.append({
                        "window_id": wid, "change_type": "CHANGED",
                        "snapshot_base": left["snapshot_no"], "snapshot_target": right["snapshot_no"],
                        "metrics": metric_diffs,
                    })
            base_abnormal = {item["window_id"] for item in self._abnormal_entries(base)}
            target_abnormal = {item["window_id"] for item in self._abnormal_entries(target)}
            return {
                "base_dataset": self._dataset_summary(base),
                "target_dataset": self._dataset_summary(target),
                "window_diffs": window_diffs,
                "validation_downstream": {
                    "base_abnormal_windows": sorted(base_abnormal),
                    "target_abnormal_windows": sorted(target_abnormal),
                    "newly_blocked_windows": sorted(target_abnormal - base_abnormal),
                    "released_windows": sorted(base_abnormal - target_abnormal),
                },
            }

    # ------------------------------------------------------------ 旧契约适配
    def current_snapshot_rows(self) -> list[dict[str, Any]]:
        """旧版 GET /api/quality 的数据来源：当前数据集（无数据集时取各窗口当前快照）。

        不再使用随机数：同一快照重复读取完全一致，页面不能自行补算缺失样本。
        """
        with self._lock:
            rows: list[dict[str, Any]] = []
            if self._state["current_dataset_id"] is not None:
                dataset = self.get_dataset(self._state["current_dataset_id"])
                for entry in dataset["entries"]:
                    for record in entry["metric_records"]:
                        row = dict(record)
                        row["窗口编号"] = entry["window_id"]
                        row["快照编号"] = entry["snapshot_no"]
                        row["生成时间"] = entry["generated_at"]
                        row["数据集版本"] = dataset["version"]
                        row["period"] = entry["period"]
                        rows.append(row)
                return rows
            for window in self._state["windows"].values():
                snapshot = self._state["snapshots"].get(window.get("current_snapshot_id"))
                if snapshot is None:
                    continue
                for record in snapshot["metric_records"]:
                    row = dict(record)
                    row["窗口编号"] = snapshot["window_id"]
                    row["快照编号"] = snapshot["snapshot_no"]
                    row["生成时间"] = snapshot["generated_at"]
                    row["period"] = snapshot["period"]
                    rows.append(row)
            return rows

    def summary_metrics(self) -> list[dict[str, Any]]:
        """KPI 卡片同样来自已封存快照，保证刷新稳定。"""
        with self._lock:
            sealed = list(self._state["snapshots"].values())
            if not sealed:
                return [
                    {"label": "覆盖数据源", "value": 0, "trend": 0},
                    {"label": "最新指标值", "value": 0, "trend": 0},
                    {"label": "异常记录", "value": 0, "trend": 0},
                ]
            latest = max(sealed, key=lambda s: s["snapshot_id"])
            records = latest["metric_records"]
            avg_rate = round(sum(r["达标率"] for r in records) / len(records), 4) if records else 0.0
            abnormal_windows = len({
                e["window_id"]
                for d in self._state["datasets"] if d["dataset_id"] == self._state["current_dataset_id"]
                for e in d["entries"]
                if any(not r["达标"] for r in e["metric_records"])
            }) if self._state["current_dataset_id"] else 0
            previous_ids = [sid for w in self._state["windows"].values() for sid in w["snapshot_ids"][:-1]]
            previous_rates = [
                r["达标率"] for sid in previous_ids for r in self._state["snapshots"][sid]["metric_records"]
            ]
            prev_avg = round(sum(previous_rates) / len(previous_rates), 4) if previous_rates else avg_rate
            trend = round((avg_rate - prev_avg) * 100, 1)
            return [
                {"label": "覆盖数据源", "value": len({s["window_id"] for s in sealed}), "trend": 0},
                {"label": "最新指标值", "value": avg_rate, "trend": trend},
                {"label": "异常记录", "value": abnormal_windows, "trend": 0},
            ]

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """旧版 POST /api/quality/run：改为读取已封存快照的确定性聚合。

        保留原字段（id/分组/指标值/对比基准/差异）与返回结构；页面不再触发补算。
        """
        period = params.get("统计周期") if isinstance(params, dict) else None
        rows = self.current_snapshot_rows()
        if period:
            period_map = {v: k for k, v in PERIOD_LABELS.items()}
            normalized = period_map.get(period, period)
            rows = [r for r in rows if r.get("period") == normalized]
        results = []
        for index, row in enumerate(rows, start=1):
            results.append({
                "id": index,
                "分组": row.get("窗口编号") or row.get("指标编号") or f"分组{index}",
                "指标值": row.get("指标值", row.get("达标率", 0)),
                "对比基准": row.get("对比基准", 0),
                "差异": row.get("差异", 0),
                "窗口编号": row.get("窗口编号"),
                "快照编号": row.get("快照编号"),
                "样本数量": row.get("样本数量"),
                "达标率": row.get("达标率"),
            })
        return results


# 进程内单例：路由层与采集校验共用同一份快照状态。
quality_snapshot_service = QualitySnapshotService()
