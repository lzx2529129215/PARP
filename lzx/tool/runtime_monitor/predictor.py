"""Online LSTM predictor adapter used by the runtime monitor."""

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


class NullPredictor:
    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def predict(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


class OnlineLSTMPredictor:
    def __init__(
        self,
        checkpoint: str | Path,
        app_vocab: str | Path,
        group_vocab: str | Path,
        user_group: str,
        top_k: int = 5,
        score_mode: str = "softmax",
        device_name: str = "auto",
    ) -> None:
        try:
            import torch
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PyTorch is required for online prediction. Install with: pip install -r requirements.txt"
            ) from exc

        from v2.infer.infer_app_lstm import build_model, load_checkpoint, multihot, score_logits, time_feature

        self.torch = torch
        self.multihot = multihot
        self.score_logits = score_logits
        self.time_feature = time_feature
        self.top_k = top_k
        self.score_mode = score_mode

        self.app_vocab = {app: int(app_id) for app, app_id in self._load_json(app_vocab).items()}
        self.group_vocab = {group: int(group_id) for group, group_id in self._load_json(group_vocab).items()}
        if user_group not in self.group_vocab:
            raise ValueError(f"unknown user group: {user_group}")
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

    def predict(self, history_apps: list[str], opened_apps: list[str], timestamp: str) -> list[dict[str, Any]]:
        return self.predict_bundle(history_apps, opened_apps, timestamp)["top_k_outputs"]

    def predict_bundle(
        self,
        history_apps: list[str],
        opened_apps: list[str],
        timestamp: str,
    ) -> dict[str, Any]:
        """Return v2 top-k rows and the complete score vector.

        The duration-aware online runner uses the same bundle contract.  This
        keeps Test2 on the already-trained test1 v2 checkpoint without making
        the older ``predict`` API incompatible.
        """
        import datetime as dt

        if not history_apps:
            return {"top_k_outputs": [], "all_probabilities": [], "probability_source": "unavailable"}
        history_ids = [self.app_vocab[app] for app in history_apps if app in self.app_vocab]
        opened_ids = [self.app_vocab[app] for app in opened_apps if app in self.app_vocab]
        if not history_ids:
            return {"top_k_outputs": [], "all_probabilities": [], "probability_source": "unavailable"}

        parsed_time = dt.datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        torch = self.torch
        batch = {
            "history_apps": torch.tensor([history_ids], dtype=torch.long, device=self.device),
            "opened_apps": torch.tensor(
                [self.multihot(opened_ids, len(self.app_vocab))],
                dtype=torch.float32,
                device=self.device,
            ),
            "time_feature": torch.tensor([self.time_feature(parsed_time)], dtype=torch.float32, device=self.device),
            "user_group": torch.tensor([self.user_group_id], dtype=torch.long, device=self.device),
        }

        rows: list[dict[str, Any]] = []
        all_rows: list[dict[str, Any]] = []
        probability_source = (
            "sigmoid_uncalibrated"
            if self.score_mode == "sigmoid"
            else "softmax_uncalibrated"
        )
        with torch.no_grad():
            outputs = self.model(
                batch["history_apps"],
                batch["opened_apps"],
                batch["time_feature"],
                batch["user_group"],
            )
            for horizon in sorted(outputs):
                scores = self.score_logits(outputs[horizon], self.score_mode)
                ranked_indices = torch.argsort(scores, dim=1, descending=True)[0].tolist()
                for rank, app_id in enumerate(ranked_indices, start=1):
                    probability = max(0.0, min(1.0, float(scores[0, app_id].item())))
                    row = {
                        "horizon": int(horizon),
                        "rank": rank,
                        "app_id": int(app_id),
                        "app": self.id_to_app[int(app_id)],
                        "raw_score": float(outputs[horizon][0, app_id].item()),
                        "probability": probability,
                        "next_use_probability": probability,
                        "next_use_probability_fixed": max(0, min(10000, int(round(probability * 10000)))),
                        "probability_source": probability_source,
                        "score_mode": self.score_mode,
                    }
                    all_rows.append(row)
                    if rank <= self.top_k:
                        rows.append(dict(row))
        return {
            "top_k_outputs": rows,
            "all_probabilities": all_rows,
            "probability_source": probability_source,
        }

    @staticmethod
    def _load_json(path: str | Path) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))
