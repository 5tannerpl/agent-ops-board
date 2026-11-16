"""Demo-mode parameters for the autoscaling live demo. Thresholds are demo values, not production tuning."""
from dataclasses import dataclass, replace

MODES = ("light", "high_concurrency")
HIGH_CONCURRENCY_UNITS = (50, 100, 150)


@dataclass(frozen=True)
class ScalingSettings:
    mode: str
    units: int
    arrival_window_seconds: float
    min_workers: int
    max_workers: int
    max_gpu_instances: int
    run_timeout_seconds: float

    poll_seconds: float = 2.0
    snapshot_seconds: float = 3.0
    scale_out_pending_4: int = 10
    scale_out_pending_8: int = 25
    idle_before_scale_in_seconds: float = 15.0
    worker_cooldown_seconds: float = 4.0
    worker_scaling_timeout_seconds: float = 60.0

    min_workers_before_gpu_scale: int = 4
    sustained_backlog_seconds: float = 10.0
    predicted_drain_gt_seconds: float = 30.0
    gpu_startup_timeout_seconds: float = 90.0
    gpu_start_cooldown_seconds: float = 30.0
    gpu_max_start_attempts: int = 2
    gpu_drain_timeout_seconds: float = 60.0
    min_gpu_free_headroom_mb: int = 1500  # free VRAM required beyond the model file size

    @property
    def intermediate_workers(self) -> int:
        return min(4, self.max_workers)


def for_mode(mode: str, units: int | None = None) -> ScalingSettings:
    if mode == "light":
        return ScalingSettings(mode=mode, units=10, arrival_window_seconds=60.0, min_workers=2, max_workers=2,
                               max_gpu_instances=1, run_timeout_seconds=300.0)
    if mode == "high_concurrency":
        units = units or 100
        if units not in HIGH_CONCURRENCY_UNITS:
            raise ValueError(f"units must be one of {HIGH_CONCURRENCY_UNITS}")
        return ScalingSettings(mode=mode, units=units, arrival_window_seconds=0.0, min_workers=2, max_workers=8,
                               max_gpu_instances=2, run_timeout_seconds=600.0)
    raise ValueError(f"unknown demo mode {mode!r}")


def tuned(settings: ScalingSettings, **changes) -> ScalingSettings:
    return replace(settings, **changes)
