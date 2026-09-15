"""Offline ablation model. Explicit features use only the visible history."""
import math

import numpy as np
import torch
from torch import nn

from .app_lstm_visit_window import AppLSTMVisitWindow

FEATURE_NAMES = [
    'last_entry_age_log3600', 'entry_observed',
    'last_exit_age_log3600', 'exit_observed',
    'entry_count_60s_div20', 'entry_count_180s_div20', 'entry_count_600s_div20',
    'occurrences_div20', 'latest_dwell_log600', 'transitions_with_current_div20',
    'history_span_log3600', 'covers_60s', 'covers_180s', 'covers_600s',
]
MODEL_TYPE = 'app_visit_window_explicit_v2_offline'


def explicit_features(app_ids, starts, anchor, num_apps):
    """Starts are true causal timestamps; the last segment is still foreground.

    An unseen event is represented by value=0, observed=0 (not a fabricated
    recent event). Counts are observed counts, with window coverage indicators.
    No current segment end time or final dwell is accepted by this interface.
    """
    if not len(app_ids) == len(starts) or not len(starts):
        raise ValueError('requires nonempty aligned visible history')
    if any(a < 0 or a >= num_apps for a in app_ids):
        raise ValueError('invalid app id')
    if any(a > b for a, b in zip(starts, starts[1:])) or starts[-1] > anchor:
        raise ValueError('noncausal or unsorted history')
    scale = lambda value, cap: math.log1p(min(max(value, 0), cap)) / math.log1p(cap)
    result = np.zeros((num_apps, len(FEATURE_NAMES)), dtype=np.float32)
    span = anchor - starts[0]
    result[:, 10:] = [scale(span, 3600), span >= 60, span >= 180, span >= 600]
    current = app_ids[-1]
    for i, (app, start) in enumerate(zip(app_ids, starts)):
        age = anchor - start
        result[app, 0:2] = [scale(age, 3600), 1]
        result[app, 4:7] += np.array([age <= w for w in (60, 180, 600)]) / 20
        result[app, 7] += 1 / 20
        result[app, 8] = scale((starts[i+1] if i+1 < len(starts) else anchor) - start, 600)
        if i+1 < len(starts):
            result[app, 2:4] = [scale(anchor - starts[i+1], 3600), 1]
            other = app_ids[i+1]
            if app != other and (app == current or other == current):
                candidate = other if app == current else app
                result[candidate, 9] += 1 / 20
    return result


class AppLSTMVisitExplicit(AppLSTMVisitWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.explicit_head = nn.Sequential(
            nn.Linear(self.num_apps * len(FEATURE_NAMES), 32), nn.ReLU(),
            nn.Linear(32, self.num_apps * 2))

    def forward(self, explicit, **features):
        logits = super().forward(**features)
        return logits + self.explicit_head(explicit.flatten(1)).reshape(-1, self.num_apps, 2)
