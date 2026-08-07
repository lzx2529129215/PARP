"""Online adapter for the duration-aware app LSTM."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


MONITOR_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = MONITOR_DIR.parent
OPERATION_PREDICTOR_ROOT = Path(
    os.environ.get(
        "OPERATION_PREDICTOR_ROOT",
        WORKSPACE_ROOT / "operation_predictor",
    )
).resolve()
if str(OPERATION_PREDICTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(OPERATION_PREDICTOR_ROOT))


class NullDurationPredictor:
    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def predict(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


class OnlineLSTMDurationPredictor:
    def __init__(
        self,
        checkpoint: str | Path,
        app_vocab: str | Path,
        group_vocab: str | Path,
        user_group: str,
        history_len: int = 5,
        duration_cap_s: float = 600.0,
        top_k: int = 5,
        score_mode: str = "sigmoid",
        device_name: str = "auto",
    ) -> None:
        try:
            import torch
        except ModuleNotFoundError as exc:
            raise RuntimeError("PyTorch is required for duration-aware online prediction.") from exc

        from v3.infer.infer_app_lstm_duration import (
            build_model,
            load_checkpoint,
            multihot,
            pad_history,
            parse_time,
            score_logits,
            time_feature,
        )

        self.torch = torch
        self.multihot = multihot
        self.pad_history = pad_history
        self.parse_time = parse_time
        self.score_logits = score_logits
        self.time_feature = time_feature
        self.history_len = int(history_len)
        self.duration_cap_s = float(duration_cap_s)
        self.top_k = int(top_k)
        self.score_mode = score_mode

        self.app_vocab = {app: int(app_id) for app, app_id in self._load_json(app_vocab).items()}
        self.group_vocab = {group: int(group_id) for group, group_id in self._load_json(group_vocab).items()}
        if user_group not in self.group_vocab:
            raise ValueError(f"unknown user group: {user_group}")
        if "<PAD>" not in self.app_vocab or "<UNKNOWN>" not in self.app_vocab:
            raise ValueError("duration app vocab must contain <PAD> and <UNKNOWN>")
        self.user_group_id = int(self.group_vocab[user_group])
        self.id_to_app = {app_id: app for app, app_id in self.app_vocab.items()}

        if device_name == "auto":
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device_name)

        checkpoint_data = load_checkpoint(checkpoint, self.device)
        if len(self.app_vocab) != int(checkpoint_data["num_apps"]):
            raise ValueError(
                f"app vocab size mismatch: vocab={len(self.app_vocab)} checkpoint={checkpoint_data['num_apps']}"
            )
        self.model = build_model(checkpoint_data, self.device)
        self.training_objective = "bce_multilabel"

    def encode_inputs(
        self,
        history_apps: list[str],
        history_durations: list[float],
        opened_apps: list[str],
        timestamp: str,
    ) -> dict[str, Any]:
        padded_apps, padded_durations, history_mask = self.pad_history(
            history_apps, history_durations, self.app_vocab, self.history_len
        )
        history_ids = [self.app_vocab[app] for app in padded_apps]
        opened_ids = [self.app_vocab.get(app, self.app_vocab["<UNKNOWN>"]) for app in opened_apps]
        parsed_time = self.parse_time(timestamp)
        torch = self.torch
        return {
            "padded_apps": padded_apps,
            "padded_durations": padded_durations,
            "history_mask": history_mask,
            "batch": {
                "history_apps": torch.tensor([history_ids], dtype=torch.long, device=self.device),
                "history_durations": torch.tensor([padded_durations], dtype=torch.float32, device=self.device),
                "history_mask": torch.tensor([history_mask], dtype=torch.float32, device=self.device),
                "opened_apps": torch.tensor(
                    [self.multihot(opened_ids, len(self.app_vocab))],
                    dtype=torch.float32,
                    device=self.device,
                ),
                "time_feature": torch.tensor([self.time_feature(parsed_time)], dtype=torch.float32, device=self.device),
                "user_group": torch.tensor([self.user_group_id], dtype=torch.long, device=self.device),
            },
        }

    def predict(
        self,
        history_apps: list[str],
        history_durations: list[float],
        opened_apps: list[str],
        timestamp: str,
    ) -> list[dict[str, Any]]:
        return self.predict_bundle(
            history_apps, history_durations, opened_apps, timestamp
        )["top_k_outputs"]

    def predict_bundle(
        self,
        history_apps: list[str],
        history_durations: list[float],
        opened_apps: list[str],
        timestamp: str,
    ) -> dict[str, Any]:
        """Return legacy Top-K rows and probabilities for every vocabulary app."""
        if not history_apps:
            return {
                "top_k_outputs": [],
                "all_probabilities": [],
                "probability_source": "unavailable",
            }
        encoded = self.encode_inputs(history_apps, history_durations, opened_apps, timestamp)
        top_k_rows: list[dict[str, Any]] = []
        all_rows: list[dict[str, Any]] = []
        torch = self.torch
        probability_source = (
            "sigmoid_uncalibrated"
            if self.score_mode == "sigmoid"
            else "softmax_uncalibrated"
        )
        with torch.no_grad():
            outputs = self.model(**encoded["batch"])
            for horizon in sorted(outputs):
                logits = outputs[horizon]
                scores = self.score_logits(logits, self.score_mode)
                ranked_indices = torch.argsort(scores, dim=1, descending=True)[0].tolist()
                for rank, app_id in enumerate(ranked_indices, start=1):
                    probability = max(0.0, min(1.0, float(scores[0, app_id].item())))
                    row = {
                        "horizon": int(horizon),
                        "rank": rank,
                        "app_id": int(app_id),
                        "app": self.id_to_app[int(app_id)],
                        "raw_score": float(logits[0, app_id].item()),
                        "probability": probability,
                        "next_use_probability": probability,
                        "next_use_probability_fixed": max(
                            0, min(10000, int(round(probability * 10000)))
                        ),
                        "probability_source": probability_source,
                        "score_mode": self.score_mode,
                    }
                    all_rows.append(row)
                    if rank <= self.top_k:
                        top_k_rows.append(dict(row))
        return {
            "top_k_outputs": top_k_rows,
            "all_probabilities": all_rows,
            "probability_source": probability_source,
        }

    @staticmethod
    def _load_json(path: str | Path) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))
