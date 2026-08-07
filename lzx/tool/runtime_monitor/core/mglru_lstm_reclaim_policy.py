"""LSTM 应用级 MGLRU 扫描预算策略的用户态配置、参考计算和写入汇总。"""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


POLICY_WRITE_FIELDS = [
    "session_id",
    "timestamp",
    "event_type",
    "app_id",
    "app_key",
    "cgroup_id",
    "probability",
    "probability_fixed",
    "probability_source",
    "ttl_ms",
    "policy_command",
    "status",
    "error",
]

VALID_MODES = {"disabled", "observe", "apply"}
MIN_SAFE_FACTOR_FIXED = 100
MAX_SAFE_FACTOR_FIXED = 2000


def _fixed_probability(value: float) -> int:
    return max(0, min(10000, int(round(float(value) * 10000))))


def _fixed_factor(value: float) -> int:
    return int(round(float(value) * 1000))


@dataclass(frozen=True)
class ReclaimPolicyConfig:
    enabled: bool
    mode: str
    foreground_factor: int
    high_threshold: int
    neutral_threshold: int
    low_threshold: int
    high_factor: int
    neutral_factor: int
    low_factor: int
    very_low_factor: int
    minimum_factor: int
    maximum_factor: int
    missing_probability: int
    unknown_factor: int
    expired_factor: int
    prediction_ttl_ms: int
    minimum_scan_pages: int
    maximum_extra_pages: int
    markov_min_probability: int

    @classmethod
    def load(cls, path: str | Path) -> "ReclaimPolicyConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        config = cls(
            enabled=bool(data.get("enabled", True)),
            mode=str(data.get("mode", "observe")),
            foreground_factor=_fixed_factor(data.get("foreground_scan_factor", 0.70)),
            high_threshold=_fixed_probability(data.get("high_probability_threshold", 0.90)),
            neutral_threshold=_fixed_probability(data.get("neutral_probability_threshold", 0.50)),
            low_threshold=_fixed_probability(data.get("low_probability_threshold", 0.20)),
            high_factor=_fixed_factor(data.get("high_probability_scan_factor", 0.75)),
            neutral_factor=_fixed_factor(data.get("neutral_probability_scan_factor", 1.00)),
            low_factor=_fixed_factor(data.get("low_probability_scan_factor", 1.10)),
            very_low_factor=_fixed_factor(data.get("very_low_probability_scan_factor", 1.25)),
            minimum_factor=_fixed_factor(data.get("minimum_scan_factor", 0.70)),
            maximum_factor=_fixed_factor(data.get("maximum_scan_factor", 1.30)),
            missing_probability=_fixed_probability(data.get("missing_prediction_probability", 0.30)),
            unknown_factor=_fixed_factor(data.get("unknown_app_scan_factor", 1.00)),
            expired_factor=_fixed_factor(data.get("expired_prediction_scan_factor", 1.00)),
            prediction_ttl_ms=int(data.get("prediction_ttl_ms", 180000)),
            minimum_scan_pages=int(data.get("minimum_scan_pages", 1)),
            maximum_extra_pages=int(data.get("maximum_extra_scan_pages_per_cycle", 4096)),
            markov_min_probability=_fixed_probability(data.get("markov_min_app_probability", 0.0)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"非法 policy mode: {self.mode}")
        if not 10000 >= self.high_threshold >= self.neutral_threshold >= self.low_threshold >= 0:
            raise ValueError("概率阈值必须满足 10000 >= high >= neutral >= low >= 0")
        factors = (
            self.foreground_factor,
            self.high_factor,
            self.neutral_factor,
            self.low_factor,
            self.very_low_factor,
            self.minimum_factor,
            self.maximum_factor,
            self.unknown_factor,
            self.expired_factor,
        )
        if any(factor < MIN_SAFE_FACTOR_FIXED or factor > MAX_SAFE_FACTOR_FIXED for factor in factors):
            raise ValueError("scan factor 必须位于安全范围 100..2000")
        if self.minimum_factor > self.maximum_factor:
            raise ValueError("minimum_scan_factor 不能大于 maximum_scan_factor")
        if self.minimum_scan_pages < 0 or self.maximum_extra_pages < 0:
            raise ValueError("扫描页边界不能为负数")
        if self.prediction_ttl_ms <= 0:
            raise ValueError("prediction_ttl_ms 必须大于 0")

    def commands(self) -> list[str]:
        mode = self.mode if self.enabled else "disabled"
        return [
            f"policy mode {mode}",
            f"policy threshold {self.high_threshold} {self.neutral_threshold} {self.low_threshold}",
            (
                f"policy factor {self.foreground_factor} {self.high_factor} "
                f"{self.neutral_factor} {self.low_factor} {self.very_low_factor}"
            ),
            (
                f"policy bounds {self.minimum_factor} {self.maximum_factor} "
                f"{self.minimum_scan_pages} {self.maximum_extra_pages}"
            ),
            (
                f"policy default {self.missing_probability} {self.unknown_factor} "
                f"{self.expired_factor} {self.markov_min_probability}"
            ),
        ]


@dataclass(frozen=True)
class ScanBudgetDecision:
    bucket: str
    factor: int
    original: int
    proposed: int
    actual: int


def choose_scan_factor(
    config: ReclaimPolicyConfig,
    probability: float | None,
    *,
    foreground: bool = False,
    expired: bool = False,
    unknown_app: bool = False,
) -> tuple[str, int]:
    if foreground:
        return "foreground", config.foreground_factor
    if unknown_app:
        return "unknown", config.unknown_factor
    if expired:
        return "expired", config.expired_factor
    if probability is None:
        fixed = config.missing_probability
        bucket = "missing"
    else:
        fixed = _fixed_probability(probability)
        bucket = ""
    if fixed >= config.high_threshold:
        factor = config.high_factor
        probability_bucket = "high"
    elif fixed >= config.neutral_threshold:
        factor = config.neutral_factor
        probability_bucket = "neutral"
    elif fixed >= config.low_threshold:
        factor = config.low_factor
        probability_bucket = "low"
    else:
        factor = config.very_low_factor
        probability_bucket = "very_low"
    return bucket or probability_bucket, factor


def calculate_scan_budget(
    original: int,
    config: ReclaimPolicyConfig,
    probability: float | None,
    *,
    foreground: bool = False,
    expired: bool = False,
    unknown_app: bool = False,
) -> ScanBudgetDecision:
    bucket, raw_factor = choose_scan_factor(
        config,
        probability,
        foreground=foreground,
        expired=expired,
        unknown_app=unknown_app,
    )
    factor = max(config.minimum_factor, min(config.maximum_factor, raw_factor))
    original = max(0, int(original))
    if original == 0:
        proposed = 0
    else:
        proposed = original * factor // 1000
        if proposed == 0:
            proposed = config.minimum_scan_pages
        if proposed > original:
            proposed = min(proposed, original + config.maximum_extra_pages)
    actual = proposed if config.mode == "apply" else original
    return ScanBudgetDecision(bucket, factor, original, proposed, actual)


class MGLRULSTMReclaimPolicyController:
    """把 JSON 策略、应用绑定和真实模型概率写入同一个 Markov debugfs。"""

    def __init__(
        self,
        *,
        enabled: bool,
        strict: bool,
        config_path: str | Path,
        session_id: str,
        model_dir: Path,
        review_dir: Path,
        debugfs_writer: Any,
    ) -> None:
        self.enabled = bool(enabled)
        self.strict = bool(strict)
        self.config_path = Path(config_path)
        self.session_id = session_id
        self.debugfs_writer = debugfs_writer
        self.csv_path = model_dir / "mglru_lstm_reclaim_policy_writes.csv"
        self.summary_path = review_dir / "mglru_lstm_reclaim_policy_summary.md"
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._writer = None
        if self.enabled:
            self._file = self.csv_path.open("w", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._file, fieldnames=POLICY_WRITE_FIELDS)
            self._writer.writeheader()
            self._file.flush()
        self.config: ReclaimPolicyConfig | None = None
        self.config_error = ""
        self.policy_config_write_ok = 0
        self.policy_config_write_error = 0
        self.app_bind_write_ok = 0
        self.app_bind_write_error = 0
        self.app_probability_write_ok = 0
        self.app_probability_write_error = 0
        self.probability_sources: set[str] = set()
        self.user_cgroup_ids: dict[str, int] = {}
        self.kernel_target_cgroup_id_last: int | None = None
        if self.enabled:
            try:
                self.config = ReclaimPolicyConfig.load(self.config_path)
            except Exception as exc:
                self.config_error = str(exc)

    @property
    def ttl_ms(self) -> int:
        return self.config.prediction_ttl_ms if self.config else 180000

    def configure_kernel(self) -> None:
        if not self.enabled or self.config is None:
            return
        for command in self.config.commands():
            _, status, error = self.debugfs_writer.write_policy_command(command)
            self._record("policy_config", policy_command=command, status=status, error=error)
            if status == "ok":
                self.policy_config_write_ok += 1
            else:
                self.policy_config_write_error += 1

    def refresh_bindings(
        self,
        apps: Iterable[Any],
        resolve_cgroup_id: Callable[[str], int | None],
        ttl_ms: int,
    ) -> None:
        if not self.enabled:
            return
        for app in apps:
            cgroup_id = resolve_cgroup_id(str(app.scope_name))
            if cgroup_id is None:
                continue
            self.user_cgroup_ids[str(app.app_key)] = cgroup_id
            command, status, error = self.debugfs_writer.write_app_binding(
                str(app.app_key), int(app.app_id), cgroup_id, ttl_ms
            )
            self._record(
                "app_bind",
                app_id=int(app.app_id),
                app_key=str(app.app_key),
                cgroup_id=cgroup_id,
                ttl_ms=ttl_ms,
                policy_command=command,
                status=status,
                error=error,
            )
            if status == "ok":
                self.app_bind_write_ok += 1
            else:
                self.app_bind_write_error += 1

    def write_probabilities(self, entries: Iterable[dict[str, Any]]) -> None:
        if not self.enabled:
            return
        for entry in entries:
            source = str(entry.get("probability_source", "unavailable"))
            self.probability_sources.add(source)
            probability = entry.get("probability")
            normalized = None if probability in (None, "") else max(0.0, min(1.0, float(probability)))
            command, status, error = self.debugfs_writer.write_app_probability(
                str(entry.get("app_key", "")),
                int(entry.get("app_id", 0)),
                normalized,
                source,
                self.ttl_ms,
            )
            self._record(
                "app_probability",
                app_id=int(entry.get("app_id", 0)),
                app_key=str(entry.get("app_key", "")),
                probability="" if normalized is None else normalized,
                probability_fixed="" if normalized is None else _fixed_probability(normalized),
                probability_source=source,
                ttl_ms=self.ttl_ms,
                policy_command=command,
                status=status,
                error=error,
            )
            if status == "ok":
                self.app_probability_write_ok += 1
            else:
                self.app_probability_write_error += 1

    def _record(self, event_type: str, **values: Any) -> None:
        if self._writer is None or self._file is None:
            return
        row = {field: "" for field in POLICY_WRITE_FIELDS}
        row.update(values)
        row.update(
            session_id=self.session_id,
            timestamp=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            event_type=event_type,
        )
        self._writer.writerow(row)
        self._file.flush()

    def probability_is_rank_based(self) -> bool:
        return "rank_based" in self.probability_sources

    def refresh_kernel_observation(self) -> None:
        for line in self.debugfs_writer.read_snapshot().splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[:2] == ["stat", "target_cgroup_id_last"]:
                try:
                    self.kernel_target_cgroup_id_last = int(parts[2])
                except ValueError:
                    self.kernel_target_cgroup_id_last = None
                return

    def cgroup_id_match(self) -> str:
        if not self.kernel_target_cgroup_id_last:
            return "unavailable"
        return str(
            self.kernel_target_cgroup_id_last in self.user_cgroup_ids.values()
        ).lower()

    def strict_result(self) -> str:
        if not self.enabled:
            return "PASS"
        if not self.strict:
            return "PASS" if not self.config_error else "PASS_WITH_WARNINGS"
        valid_sources = self.probability_sources - {"rank_based", "unavailable"}
        ok = (
            self.config is not None
            and not self.config_error
            and self.policy_config_write_ok == 5
            and self.policy_config_write_error == 0
            and self.app_bind_write_ok > 0
            and self.app_probability_write_ok > 0
            and self.app_probability_write_error == 0
            and bool(valid_sources)
            and not self.probability_is_rank_based()
            and (
                self.config.mode != "apply"
                or self.cgroup_id_match() == "true"
            )
        )
        return "PASS" if ok else "FAIL"

    def write_summary(self) -> None:
        self.refresh_kernel_observation()
        sources = ",".join(sorted(self.probability_sources)) or "none"
        mode = self.config.mode if self.config else "unavailable"
        lines = [
            "# MGLRU LSTM 应用级回收策略汇总",
            "",
            f"- policy_enabled: {str(self.enabled).lower()}",
            f"- policy_mode: {mode}",
            f"- policy_config: `{self.config_path}`",
            f"- policy_config_loaded: {str(self.config is not None).lower()}",
            f"- policy_config_error: {self.config_error or 'none'}",
            f"- policy_config_write_ok: {self.policy_config_write_ok}",
            f"- policy_config_write_error: {self.policy_config_write_error}",
            f"- app_bind_write_ok: {self.app_bind_write_ok}",
            f"- app_bind_write_error: {self.app_bind_write_error}",
            f"- app_probability_write_ok: {self.app_probability_write_ok}",
            f"- app_probability_write_error: {self.app_probability_write_error}",
            f"- probability_source: {sources}",
            f"- probability_is_rank_based: {str(self.probability_is_rank_based()).lower()}",
            f"- user_cgroup_ids: {json.dumps(self.user_cgroup_ids, ensure_ascii=False, sort_keys=True)}",
            f"- kernel_target_cgroup_id_last: {self.kernel_target_cgroup_id_last or 'unavailable'}",
            f"- user_kernel_cgroup_id_match: {self.cgroup_id_match()}",
            f"- strict_result: {self.strict_result()}",
            f"- final_result: {self.strict_result()}",
        ]
        self.summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def close(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
        self.write_summary()
