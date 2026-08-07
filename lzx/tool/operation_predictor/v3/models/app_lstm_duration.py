"""Duration-aware application LSTM model.

This v3 model keeps app transitions and foreground dwell time as separate
inputs. It does not encode duration by repeating app tokens.
"""

from __future__ import annotations

import math

import torch
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence


class AppLSTMDurationV3(nn.Module):
    """Encode app state segments with explicit dwell duration features."""

    def __init__(
        self,
        num_apps: int,
        num_user_groups: int,
        horizons: list[int],
        pad_id: int,
        app_embedding_dim: int = 32,
        duration_embedding_dim: int = 8,
        group_embedding_dim: int = 8,
        hidden_dim: int = 64,
        opened_dim: int = 32,
        duration_cap_s: float = 600.0,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        self.num_apps = int(num_apps)
        self.horizons = [int(horizon) for horizon in horizons]
        self.pad_id = int(pad_id)
        self.duration_cap_s = float(duration_cap_s)

        self.app_embedding = nn.Embedding(num_apps, app_embedding_dim, padding_idx=self.pad_id)
        self.duration_projection = nn.Sequential(
            nn.Linear(1, duration_embedding_dim),
            nn.ReLU(),
        )
        self.lstm = nn.LSTM(app_embedding_dim + duration_embedding_dim, hidden_dim, batch_first=True)
        self.opened_encoder = nn.Sequential(
            nn.Linear(num_apps, opened_dim),
            nn.ReLU(),
        )
        self.group_embedding = nn.Embedding(num_user_groups, group_embedding_dim)

        feature_dim = hidden_dim + opened_dim + group_embedding_dim + 3
        self.shared = nn.Sequential(
            nn.Linear(feature_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
        )
        self.heads = nn.ModuleDict({str(horizon): nn.Linear(hidden_dim, num_apps) for horizon in self.horizons})

    def normalize_duration(self, history_durations: torch.Tensor) -> torch.Tensor:
        capped = torch.clamp(history_durations.float(), min=0.0, max=self.duration_cap_s)
        denom = math.log1p(self.duration_cap_s)
        return torch.log1p(capped) / denom

    def forward(
        self,
        history_apps: torch.Tensor,
        history_durations: torch.Tensor,
        history_mask: torch.Tensor,
        opened_apps: torch.Tensor,
        time_feature: torch.Tensor,
        user_group: torch.Tensor,
    ) -> dict[int, torch.Tensor]:
        app_emb = self.app_embedding(history_apps)
        duration_norm = self.normalize_duration(history_durations).unsqueeze(-1)
        duration_emb = self.duration_projection(duration_norm)
        sequence = torch.cat([app_emb, duration_emb], dim=2)
        sequence = sequence * history_mask.float().unsqueeze(-1)

        lengths = history_mask.long().sum(dim=1)
        has_history = lengths > 0
        packed_lengths = torch.clamp(lengths, min=1).cpu()
        packed = pack_padded_sequence(sequence, packed_lengths, batch_first=True, enforce_sorted=False)
        _, (hidden, _) = self.lstm(packed)
        history_repr = hidden[-1] * has_history.float().unsqueeze(1)

        opened_repr = self.opened_encoder(opened_apps)
        group_repr = self.group_embedding(user_group)
        features = torch.cat([history_repr, opened_repr, time_feature, group_repr], dim=1)
        shared = self.shared(features)
        return {horizon: self.heads[str(horizon)](shared) for horizon in self.horizons}


class AppLSTMNextV3(nn.Module):
    """V3 single-step app-switch model with one whitelist probability vector.

    Unlike ``AppLSTMDurationV3``, this head has no time horizon dimension. It
    predicts the next foreground whitelist App from the duration-aware
    history, opened-App features, wall-clock features, user group and the
    currently observed foreground App.
    """

    def __init__(
        self,
        num_apps: int,
        num_user_groups: int,
        pad_id: int,
        app_embedding_dim: int = 32,
        duration_embedding_dim: int = 8,
        group_embedding_dim: int = 8,
        hidden_dim: int = 64,
        opened_dim: int = 32,
        duration_cap_s: float = 600.0,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        self.num_apps = int(num_apps)
        self.pad_id = int(pad_id)
        self.duration_cap_s = float(duration_cap_s)
        self.app_embedding = nn.Embedding(num_apps, app_embedding_dim, padding_idx=self.pad_id)
        self.duration_projection = nn.Sequential(
            nn.Linear(1, duration_embedding_dim),
            nn.ReLU(),
        )
        self.lstm = nn.LSTM(app_embedding_dim + duration_embedding_dim, hidden_dim, batch_first=True)
        self.opened_encoder = nn.Sequential(
            nn.Linear(num_apps, opened_dim),
            nn.ReLU(),
        )
        self.group_embedding = nn.Embedding(num_user_groups, group_embedding_dim)
        feature_dim = hidden_dim + opened_dim + group_embedding_dim + 3 + app_embedding_dim
        self.shared = nn.Sequential(
            nn.Linear(feature_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
        )
        self.output = nn.Linear(hidden_dim, num_apps)

    def normalize_duration(self, history_durations: torch.Tensor) -> torch.Tensor:
        capped = torch.clamp(history_durations.float(), min=0.0, max=self.duration_cap_s)
        denom = math.log1p(self.duration_cap_s)
        return torch.log1p(capped) / denom

    def forward(
        self,
        history_apps: torch.Tensor,
        history_durations: torch.Tensor,
        history_mask: torch.Tensor,
        opened_apps: torch.Tensor,
        time_feature: torch.Tensor,
        user_group: torch.Tensor,
        current_app: torch.Tensor,
    ) -> torch.Tensor:
        app_emb = self.app_embedding(history_apps)
        duration_norm = self.normalize_duration(history_durations).unsqueeze(-1)
        duration_emb = self.duration_projection(duration_norm)
        sequence = torch.cat([app_emb, duration_emb], dim=2)
        sequence = sequence * history_mask.float().unsqueeze(-1)

        lengths = history_mask.long().sum(dim=1)
        has_history = lengths > 0
        packed_lengths = torch.clamp(lengths, min=1).cpu()
        packed = pack_padded_sequence(sequence, packed_lengths, batch_first=True, enforce_sorted=False)
        _, (hidden, _) = self.lstm(packed)
        history_repr = hidden[-1] * has_history.float().unsqueeze(1)

        opened_repr = self.opened_encoder(opened_apps)
        group_repr = self.group_embedding(user_group)
        current_repr = self.app_embedding(current_app)
        features = torch.cat(
            [history_repr, opened_repr, time_feature, group_repr, current_repr],
            dim=1,
        )
        return self.output(self.shared(features))
