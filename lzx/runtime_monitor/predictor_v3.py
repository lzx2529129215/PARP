"""Runtime adapter for the v3 single-step application-switch LSTM."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MONITOR_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = MONITOR_DIR.parent
OPERATION_PREDICTOR_ROOT = Path(
    os.environ.get("OPERATION_PREDICTOR_ROOT", WORKSPACE_ROOT / "operation_predictor")
).resolve()
if str(OPERATION_PREDICTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(OPERATION_PREDICTOR_ROOT))


class OnlineLSTMNextV3Predictor:
    """Predict one masked-softmax probability for every whitelist App."""

    def __init__(
        self,
        checkpoint: str | Path,
        app_vocab: str | Path,
        group_vocab: str | Path,
        user_group: str,
        top_k: int = 5,
        device_name: str = "auto",
    ) -> None:
        try:
            import torch
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError("PyTorch is required for online v3 prediction") from exc

        from v3.models.app_lstm_duration import AppLSTMNextV3

        self.torch = torch
        self.top_k = max(1, int(top_k))
        self.app_vocab = self._load_vocab(app_vocab)
        self.group_vocab = self._load_vocab(group_vocab)
        if user_group not in self.group_vocab:
            raise ValueError(f"unknown user group: {user_group}")
        self.user_group_id = int(self.group_vocab[user_group])
        self.id_to_app = {app_id: app for app, app_id in self.app_vocab.items()}
        self.pad_id = int(self.app_vocab.get("<PAD>", -1))
        self.unknown_id = int(self.app_vocab.get("<UNKNOWN>", -1))
        self.whitelist_ids = [
            int(app_id)
            for app, app_id in sorted(self.app_vocab.items(), key=lambda item: int(item[1]))
            if app not in {"<PAD>", "<UNKNOWN>"}
        ]

        self.device = (
            torch.device("cuda" if torch.cuda.is_available() else "cpu")
            if device_name == "auto"
            else torch.device(device_name)
        )
        checkpoint_data = self._load_checkpoint(checkpoint)
        if checkpoint_data.get("model_type") not in {None, "app_switch_v3"}:
            raise ValueError(
                f"checkpoint is not a v3 single-step model: {checkpoint_data.get('model_type')}"
            )
        if len(self.app_vocab) != int(checkpoint_data["num_apps"]):
            raise ValueError(
                f"v3 vocab size mismatch: vocab={len(self.app_vocab)} "
                f"checkpoint={checkpoint_data['num_apps']}"
            )
        ckpt_args = checkpoint_data.get("args", {})
        self.model = AppLSTMNextV3(
            num_apps=int(checkpoint_data["num_apps"]),
            num_user_groups=int(checkpoint_data["num_user_groups"]),
            pad_id=int(checkpoint_data.get("pad_id", self.pad_id)),
            app_embedding_dim=int(ckpt_args.get("app_embedding_dim", 32)),
            duration_embedding_dim=int(ckpt_args.get("duration_embedding_dim", 8)),
            group_embedding_dim=int(ckpt_args.get("group_embedding_dim", 8)),
            hidden_dim=int(ckpt_args.get("hidden_dim", 64)),
            opened_dim=int(ckpt_args.get("opened_dim", 32)),
            duration_cap_s=float(ckpt_args.get("duration_cap_s", checkpoint_data.get("duration_cap_s", 600.0))),
            dropout=float(ckpt_args.get("dropout", 0.2)),
        ).to(self.device)
        self.model.load_state_dict(checkpoint_data["model_state_dict"])
        self.model.eval()

    @staticmethod
    def _load_vocab(path: str | Path) -> dict[str, int]:
        return {key: int(value) for key, value in json.loads(Path(path).read_text(encoding="utf-8")).items()}

    def _load_checkpoint(self, path: str | Path) -> dict[str, Any]:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"v3 checkpoint not found: {candidate}")
        try:
            return self.torch.load(candidate, map_location=self.device, weights_only=False)
        except TypeError:  # older torch
            return self.torch.load(candidate, map_location=self.device)

    def predict_bundle(
        self,
        history_apps: list[str],
        history_durations: list[str | float],
        history_mask: list[str | int],
        opened_apps: list[str],
        current_app: str,
        timestamp: str,
    ) -> dict[str, Any]:
        import datetime as dt

        if not history_apps:
            return {"top_k_outputs": [], "all_probabilities": [], "probability_source": "unavailable"}
        if not (len(history_apps) == len(history_durations) == len(history_mask)):
            raise ValueError("v3 history apps, durations and mask must have equal lengths")

        app_ids = [self.app_vocab.get(app, self.unknown_id) for app in history_apps]
        durations = [max(0.0, float(value)) for value in history_durations]
        masks = [float(value) for value in history_mask]
        opened_ids = [self.app_vocab[app] for app in opened_apps if app in self.app_vocab]
        current_id = self.app_vocab.get(current_app, self.unknown_id)
        parsed_time = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        weekday = parsed_time.weekday()
        time_vector = [float(parsed_time.hour) / 23.0, float(weekday) / 6.0, float(weekday >= 5)]

        torch = self.torch
        batch = {
            "history_apps": torch.tensor([app_ids], dtype=torch.long, device=self.device),
            "history_durations": torch.tensor([durations], dtype=torch.float32, device=self.device),
            "history_mask": torch.tensor([masks], dtype=torch.float32, device=self.device),
            "opened_apps": torch.tensor([self._multihot(opened_ids)], dtype=torch.float32, device=self.device),
            "time_feature": torch.tensor([time_vector], dtype=torch.float32, device=self.device),
            "user_group": torch.tensor([self.user_group_id], dtype=torch.long, device=self.device),
            "current_app": torch.tensor([current_id], dtype=torch.long, device=self.device),
        }
        with torch.no_grad():
            logits = self.model(**batch)[0]

        candidate_ids = [app_id for app_id in self.whitelist_ids if app_id != current_id]
        if not candidate_ids:
            return {"top_k_outputs": [], "all_probabilities": [], "probability_source": "unavailable"}
        candidate_tensor = torch.tensor(candidate_ids, dtype=torch.long, device=self.device)
        candidate_logits = logits[candidate_tensor]
        probabilities = torch.softmax(candidate_logits, dim=0)
        ranked = torch.argsort(probabilities, descending=True).tolist()
        all_rows: list[dict[str, Any]] = []
        for rank, position in enumerate(ranked, start=1):
            app_id = candidate_ids[position]
            probability = float(probabilities[position].item())
            all_rows.append({
                "rank": rank,
                "app_id": app_id,
                "app": self.id_to_app[app_id],
                "raw_logit": float(candidate_logits[position].item()),
                "probability": probability,
                "next_use_probability": probability,
                "next_use_probability_fixed": max(0, min(10000, int(round(probability * 10000)))),
                "probability_source": "softmax_whitelist_masked",
                "score_mode": "softmax",
                "prediction_format": "app_probability",
            })
        return {
            "top_k_outputs": all_rows[: self.top_k],
            "all_probabilities": all_rows,
            "probability_source": "softmax_whitelist_masked",
            "prediction_format": "app_probability",
        }

    def _multihot(self, ids: list[int]) -> list[float]:
        vector = [0.0] * len(self.app_vocab)
        for app_id in ids:
            if 0 <= app_id < len(vector):
                vector[app_id] = 1.0
        return vector
