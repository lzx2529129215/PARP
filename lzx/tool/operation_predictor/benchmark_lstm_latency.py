#!/usr/bin/env python3
"""Benchmark single-inference latency for the v2 application LSTM model."""

from __future__ import annotations

import sys
import time
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch

from v2.models.app_lstm import AppLSTMV2
from src.utils.io_utils import load_json


def load_checkpoint(path: str | Path, device: torch.device) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"checkpoint not found: {path}")
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        return torch.load(path, map_location=device)


def build_model(checkpoint: dict, device: torch.device) -> AppLSTMV2:
    ckpt_args = checkpoint.get("args", {})
    horizons = [int(h) for h in checkpoint.get("horizons", ckpt_args.get("horizons", [3, 5, 10]))]
    model = AppLSTMV2(
        num_apps=int(checkpoint["num_apps"]),
        num_user_groups=int(checkpoint["num_user_groups"]),
        horizons=horizons,
        app_embedding_dim=int(ckpt_args.get("app_embedding_dim", 32)),
        group_embedding_dim=int(ckpt_args.get("group_embedding_dim", 8)),
        hidden_dim=int(ckpt_args.get("hidden_dim", 64)),
        opened_dim=int(ckpt_args.get("opened_dim", 32)),
        dropout=float(ckpt_args.get("dropout", 0.2)),
    ).to(device)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    return model


def make_dummy_input(
    num_apps: int, history_len: int, device: torch.device
) -> dict[str, torch.Tensor]:
    """Create a single dummy sample matching the model's expected input shapes."""
    return {
        "history_apps": torch.randint(0, num_apps, (1, history_len), dtype=torch.long, device=device),
        "opened_apps": torch.rand(1, num_apps, dtype=torch.float32, device=device),
        "time_feature": torch.rand(1, 3, dtype=torch.float32, device=device),
        "user_group": torch.randint(0, 10, (1,), dtype=torch.long, device=device),
    }


@torch.no_grad()
def benchmark(
    model: AppLSTMV2,
    inputs: dict[str, torch.Tensor],
    warmup: int = 50,
    repeat: int = 1000,
    device: torch.device = torch.device("cpu"),
) -> dict:
    # Warmup
    for _ in range(warmup):
        _ = model(
            inputs["history_apps"],
            inputs["opened_apps"],
            inputs["time_feature"],
            inputs["user_group"],
        )

    # Measure
    if device.type == "cuda":
        times: list[float] = []
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        for _ in range(repeat):
            start.record()
            _ = model(
                inputs["history_apps"],
                inputs["opened_apps"],
                inputs["time_feature"],
                inputs["user_group"],
            )
            end.record()
            torch.cuda.synchronize()
            times.append(start.elapsed_time(end))  # ms
    else:
        # CPU: use time.perf_counter
        times: list[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            _ = model(
                inputs["history_apps"],
                inputs["opened_apps"],
                inputs["time_feature"],
                inputs["user_group"],
            )
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000.0)  # ms

    times_sorted = sorted(times)
    n = len(times_sorted)

    def percentile(p: float) -> float:
        idx = int(p / 100.0 * (n - 1))
        return times_sorted[idx]

    return {
        "device": device.type,
        "warmup_iters": warmup,
        "measure_iters": repeat,
        "mean_ms": sum(times) / n,
        "median_ms": percentile(50),
        "p50_ms": percentile(50),
        "p90_ms": percentile(90),
        "p95_ms": percentile(95),
        "p99_ms": percentile(99),
        "min_ms": times_sorted[0],
        "max_ms": times_sorted[-1],
    }


def main() -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device}")

    # Load checkpoint
    checkpoint_path = Path("outputs/checkpoints/app_lstm/app_lstm.pt")
    print(f"loading checkpoint: {checkpoint_path}")
    checkpoint = load_checkpoint(checkpoint_path, device)

    model = build_model(checkpoint, device)
    num_apps = int(checkpoint["num_apps"])
    print(f"num_apps: {num_apps}, horizons: {model.horizons}")

    # Count parameters
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"total parameters: {total_params:,}")
    print(f"trainable parameters: {trainable_params:,}")

    # Build dummy input (history_len=10, a typical sequence length)
    inputs = make_dummy_input(num_apps=num_apps, history_len=10, device=device)

    print(f"\nbenchmarking single-sample inference (batch_size=1)...")
    print(f"warming up 50 iterations, then measuring 2000 iterations...")

    stats = benchmark(
        model,
        inputs,
        warmup=50,
        repeat=2000,
        device=device,
    )

    print("\n--- Latency Results (single sample, batch_size=1) ---")
    print(f"  Device:       {stats['device']}")
    print(f"  Mean:         {stats['mean_ms']:.4f} ms")
    print(f"  Median (p50): {stats['median_ms']:.4f} ms")
    print(f"  P90:          {stats['p90_ms']:.4f} ms")
    print(f"  P95:          {stats['p95_ms']:.4f} ms")
    print(f"  P99:          {stats['p99_ms']:.4f} ms")
    print(f"  Min:          {stats['min_ms']:.4f} ms")
    print(f"  Max:          {stats['max_ms']:.4f} ms")

    # Also print fps equivalent
    if stats["mean_ms"] > 0:
        fps = 1000.0 / stats["mean_ms"]
        print(f"\n  Throughput:   {fps:.1f} predictions/second")

    print("\n--- Raw data (first 10 runs, ms) ---")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
