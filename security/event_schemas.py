"""Event allowlist and strict payload schemas."""
from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StatusPayload(_StrictModel):
    status: str | None = None
    seconds: float | None = None


class _ScalingStartedPayload(_StrictModel):
    mode: str | None = None
    planned_units: int | None = None


class _WorkerScalingPayload(_StrictModel):
    from_: int | None = Field(default=None, alias="from")
    to: int | None = None
    reason: str | None = None
    status: str | None = None
    actual_workers: int | None = None


class _GpuPayload(_StrictModel):
    gpu_id: str | int | None = None
    reason: str | None = None
    error_code: str | None = None
    load_duration_seconds: float | None = None
    inflight_requests: int | None = None
    exit_code: int | None = None


class _RoutePayload(_StrictModel):
    active_backends: list[str] | None = None
    reason: str | None = None


class _ControllerErrorPayload(_StrictModel):
    error: str | None = None


class _ScalingFailedPayload(_StrictModel):
    error_code: str | None = None
    phase: str | None = None
    message: str | None = None


class _GpuSnapshot(_StrictModel):
    state: str | None = None
    port: int | None = None
    memory_used_mb: float | None = None
    utilization_pct: float | None = None


class _SnapshotPayload(_StrictModel):
    mode: str | None = None
    pending: int | None = None
    running: int | None = None
    completed: int | None = None
    failed: int | None = None
    total: int | None = None
    actual_workers: int | None = None
    desired_workers: int | None = None
    gpu_instances_ready: int | None = None
    scaling_state: str | None = None
    gpu0: _GpuSnapshot | None = None
    gpu1: _GpuSnapshot | None = None
    requests_by_gpu: dict[str, int | None] | None = None
    inflight_by_gpu: dict[str, int | None] | None = None
    units_by_gpu: dict[str, int | None] | None = None
    unattributed_units: int | None = None


class _SummaryPayload(_StrictModel):
    status: str | None = None
    completed: int | None = None
    total: int | None = None
    failed: int | None = None
    retries: int | None = None
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    throughput_per_min: float | None = None
    peak_workers: int | None = None
    final_workers: int | None = None
    confirmed_worker_levels: list[int] | None = None
    peak_gpu_instances: int | None = None
    final_gpu_instances: int | None = None
    gpu0_units_completed: int | None = None
    gpu1_units_completed: int | None = None
    unattributed_units: int | None = None
    gpu1_load_seconds: float | None = None
    gpu1_stopped_cleanly: bool | None = None
    scale_out_seconds: float | None = None
    scale_in_seconds: float | None = None
    telemetry_failed_events: int | None = None
    reasons: list[str] | None = None


_EMPTY = _StrictModel
EVENT_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "notebook_started": _StatusPayload,
    "environment_ready": _StatusPayload,
    "model_located": _StatusPayload,
    "model_ready": _StatusPayload,
    "model_validation_started": _StatusPayload,
    "worker_validation_completed": _StatusPayload,
    "modelcheck_completed": _StatusPayload,
    "model_validation_completed": _StatusPayload,
    "scaling_demo_started": _ScalingStartedPayload,
    "worker_scaling_requested": _WorkerScalingPayload,
    "worker_scaling_completed": _WorkerScalingPayload,
    "gpu_instance_starting": _GpuPayload,
    "gpu_instance_ready": _GpuPayload,
    "gpu_instance_failed": _GpuPayload,
    "inference_route_changed": _RoutePayload,
    "gpu_instance_draining": _GpuPayload,
    "gpu_instance_stopped": _GpuPayload,
    "scaling_controller_error": _ControllerErrorPayload,
    "scaling_demo_completed": _SummaryPayload,
    "scaling_demo_failed": _ScalingFailedPayload,
    "scaling_metrics_snapshot": _SnapshotPayload,
}


def validate_payload(event_type: str, payload: dict) -> dict:
    model = EVENT_PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise ValueError("unknown event type")
    return model.model_validate(payload).model_dump(exclude_none=True, by_alias=True)
