"""Nested entry probabilities with the v3 duration-aware input contract."""
from __future__ import annotations

import datetime as dt
import torch
from torch import nn
from torch.nn import functional as F

from .app_lstm_duration import AppLSTMNextV3

MODEL_TYPE = "app_visit_window_v1"
FORMAT = "visit_window"
HORIZONS = (30, 180)
VISIT_DEFINITION = "foreground_entry_strictly_after_anchor_inclusive_deadline"


def encode_features(history_apps, history_durations, history_mask, opened_apps,
                    current_app, timestamp, user_group, app_vocab, group_vocab,
                    history_len=5):
    """The same causal encoding is used in training, replay and live inference."""
    if not len(history_apps) == len(history_durations) == len(history_mask):
        raise ValueError("history arrays must have equal lengths")
    import math
    if any(float(m) not in (0, 1) for m in history_mask):
        raise ValueError("history mask must be binary")
    if any(not math.isfinite(float(d)) or float(d) < 0 for d in history_durations):
        raise ValueError("durations must be finite and nonnegative")
    valid = [(a, float(d)) for a, d, m in zip(history_apps, history_durations, history_mask) if float(m)]
    valid = valid[-history_len:]
    pad = history_len - len(valid)
    unknown = app_vocab["<UNKNOWN>"]
    when = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    opened = [0.] * len(app_vocab)
    for app in opened_apps:
        if app in app_vocab and not app.startswith("<"):
            opened[app_vocab[app]] = 1.
    group_id = group_vocab[user_group] if isinstance(user_group, str) and user_group in group_vocab else int(user_group)
    if group_id not in group_vocab.values():
        raise ValueError("unknown user group")
    return {
        "history_apps": [app_vocab["<PAD>"]] * pad + [app_vocab.get(a, unknown) for a, _ in valid],
        "history_durations": [0.] * pad + [d for _, d in valid],
        "history_mask": [0.] * pad + [1.] * len(valid),
        "opened_apps": opened,
        "time_feature": [when.hour / 23., when.weekday() / 6., float(when.weekday() >= 5)],
        "user_group": group_id,
        "current_app": app_vocab.get(current_app, unknown),
    }


def tensor_features(features, device="cpu"):
    integer = {"history_apps", "user_group", "current_app"}
    return {key: torch.tensor([value], dtype=torch.long if key in integer else torch.float32,
                              device=device) for key, value in features.items()}


def log_probabilities(logits):
    z1, z2 = logits.unbind(-1)
    log_p30, log_n30 = F.logsigmoid(z1), F.logsigmoid(-z1)
    log_p180 = torch.logaddexp(log_p30, log_n30 + F.logsigmoid(z2))
    log_n180 = log_n30 + F.logsigmoid(-z2)
    return torch.stack((log_p30, log_p180), -1), torch.stack((log_n30, log_n180), -1)


def visit_probabilities(logits):
    p30, q = torch.sigmoid(logits).unbind(-1)
    return torch.stack((p30, p30 + (1. - p30) * q), -1)


def masked_loss(logits, labels, valid):
    log_p, log_n = log_probabilities(logits)
    losses = -(labels * log_p + (1. - labels) * log_n) * valid
    counts = valid.sum(dim=(0, 1))
    active = counts > 0
    per_window = losses.sum(dim=(0, 1)) / counts.clamp_min(1.)
    return (per_window * active).sum() / active.sum().clamp_min(1)


class AppLSTMVisitWindow(AppLSTMNextV3):
    """Reuses the v3 encoder; changes only output and corrects padded packing."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.output = nn.Linear(self.output.in_features, self.num_apps * 2)

    def forward(self, history_apps, history_durations, history_mask, opened_apps,
                time_feature, user_group, current_app):
        # pack_padded_sequence requires valid tokens at the LEFT. Public input
        # stays left-padded, but compact valid tokens before invoking the encoder.
        positions = torch.arange(history_apps.shape[1], device=history_apps.device)[None, :]
        order = torch.argsort(positions + (1 - history_mask.long()) * history_apps.shape[1], dim=1)
        args = [value.gather(1, order) for value in (history_apps, history_durations, history_mask)]
        logits = super().forward(*args, opened_apps, time_feature, user_group, current_app)
        return logits.reshape(-1, self.num_apps, 2)
