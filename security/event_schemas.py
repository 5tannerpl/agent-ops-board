"""Event allowlist and strict, event-specific payload schemas.

Every schema forbids unknown fields. Strings are short single-line values: raw logs (control characters / newlines),
URLs and credential-like tokens are rejected, so stdout/stderr, endpoints and secrets cannot be smuggled into a
payload under an allowed field name. Producers pass free text through ``security.event_text.safe_text`` first.
"""
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from pydantic_core import PydanticCustomError

from security.event_text import is_unsafe


def _reject_unsafe(value: str) -> str:
    if is_unsafe(value):
        raise PydanticCustomError("unsafe_text", "text contains a raw log, URL or credential-like value")
    return value


Text = Annotated[str, StringConstraints(max_length=300), AfterValidator(_reject_unsafe)]
Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:+-]{1,64}$")]
Count = Annotated[int, Field(ge=0, le=10_000_000)]
Seconds = Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)]
EpochSeconds = Annotated[float, Field(ge=0, le=4_102_444_800, allow_inf_nan=False)]
Megabytes = Annotated[float, Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)]
Percent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Port = Annotated[int, Field(ge=1, le=65535)]
GpuId = Annotated[int, Field(ge=0, le=15)]
ExitCode = Annotated[int, Field(ge=-255, le=255)]
HttpStatus = Annotated[int, Field(ge=100, le=599)]
ValidationStatus = Literal["passed", "failed"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ---- Notebook / model validation ------------------------------------------------------------------------------

class _StatusPayload(_StrictModel):
    status: Token | None = None
    seconds: Seconds | None = None


class _NotebookStartedPayload(_StrictModel):
    status: Literal["running"] | None = None
    gpu_count: Count | None = None
    gpus: list[Text] | None = Field(default=None, max_length=16)


class _ModelReadyPayload(_StrictModel):
    api_health: HttpStatus | None = None
    backend: Token | None = None
    status: Token | None = None
    seconds: Seconds | None = None


class _ModelValidationStartedPayload(_StrictModel):
    status: Literal["running"]
    test_runs: Count | None = None
    model: Token | None = None


class _WorkerValidationCompletedPayload(_StrictModel):
    status: ValidationStatus
    test_runs: Count | None = None
    duration_seconds: Seconds | None = None
    exit_code: ExitCode | None = None


class _ModelcheckCompletedPayload(_StrictModel):
    status: ValidationStatus
    duration_seconds: Seconds | None = None
    exit_code: ExitCode | None = None


class _ModelValidationCompletedPayload(_StrictModel):
    status: ValidationStatus
    model: Token | None = None
    backend: Token | None = None
    api_health: HttpStatus | None = None


# ---- Scaling demo ---------------------------------------------------------------------------------------------

class _ScalingStartedPayload(_StrictModel):
    mode: Token | None = None
    planned_units: Count | None = None
    min_workers: Count | None = None
    max_workers: Count | None = None
    initial_gpu_instances: Count | None = None
    target_gpu_instances: Count | None = None
    arrival_window_seconds: Seconds | None = None
    gpu_count: Count | None = None
    gpu1_available: bool | None = None
    model: Token | None = None


class _WorkerScalingRequestedPayload(_StrictModel):
    from_: Count | None = Field(default=None, alias="from")
    to: Count | None = None
    reason: Text | None = None
    queue_depth: Count | None = None
    cooldown_until: EpochSeconds | None = None


class _WorkerScalingCompletedPayload(_StrictModel):
    from_: Count | None = Field(default=None, alias="from")
    to: Count | None = None
    status: Literal["confirmed", "timeout", "superseded"] | None = None
    desired_workers: Count | None = None
    actual_workers: Count | None = None
    duration_seconds: Seconds | None = None


class _GpuStartingPayload(_StrictModel):
    gpu_id: GpuId | None = None
    port: Port | None = None
    reason: Text | None = None


class _GpuReadyPayload(_StrictModel):
    gpu_id: GpuId | None = None
    port: Port | None = None
    model: Token | None = None
    status: Literal["ready"] | None = None
    load_duration_seconds: Seconds | None = None
    memory_used_mb: Megabytes | None = None
    memory_delta_mb: Megabytes | None = None
    device_binding: Text | None = None
    device_binding_verified: bool | None = None
    adopted: bool | None = None


class _GpuFailedPayload(_StrictModel):
    gpu_id: GpuId | None = None
    port: Port | None = None
    error_code: Token | None = None
    message: Text | None = None
    attempt: Count | None = None
    status: Literal["failed"] | None = None


class _GpuDrainingPayload(_StrictModel):
    gpu_id: GpuId | None = None
    inflight_requests: Count | None = None


class _GpuStoppedPayload(_StrictModel):
    gpu_id: GpuId | None = None
    port: Port | None = None
    exit_code: ExitCode | None = None
    reason: Text | None = None
    inflight_requests: Count | None = None
    port_released: bool | None = None
    memory_released: bool | None = None
    clean: bool | None = None
    status: Literal["stopped"] | None = None


class _RoutePayload(_StrictModel):
    active_backends: list[Token] | None = Field(default=None, max_length=8)
    reason: Text | None = None


class _ControllerErrorPayload(_StrictModel):
    error: Token | None = None
    message: Text | None = None


class _ScalingFailedPayload(_StrictModel):
    status: Literal["failed"] | None = None
    mode: Token | None = None
    error_code: Token | None = None
    phase: Token | None = None
    message: Text | None = None


class _GpuSnapshot(_StrictModel):
    state: Token | None = None
    port: Port | None = None
    memory_used_mb: Megabytes | None = None
    utilization_pct: Percent | None = None


_PerGpuCounts = dict[Token, Count | None]


class _SnapshotPayload(_StrictModel):
    mode: Token | None = None
    pending: Count | None = None
    running: Count | None = None
    completed: Count | None = None
    failed: Count | None = None
    total: Count | None = None
    actual_workers: Count | None = None
    desired_workers: Count | None = None
    gpu_instances_ready: Count | None = None
    scaling_state: Token | None = None
    gpu0: _GpuSnapshot | None = None
    gpu1: _GpuSnapshot | None = None
    requests_by_gpu: _PerGpuCounts | None = None
    inflight_by_gpu: _PerGpuCounts | None = None
    failed_by_gpu: _PerGpuCounts | None = None
    units_by_gpu: _PerGpuCounts | None = None
    unattributed_units: Count | None = None
    oldest_pending_seconds: Seconds | None = None
    estimated_drain_seconds: Seconds | None = None


class _SummaryPayload(_StrictModel):
    status: Literal["passed", "degraded", "failed"]
    mode: Token | None = None
    completed: Count | None = None
    total: Count | None = None
    failed: Count | None = None
    retries: Count | None = None
    duration_seconds: Seconds | None = None
    p50_seconds: Seconds | None = None
    p95_seconds: Seconds | None = None
    throughput_per_min: Seconds | None = None
    peak_workers: Count | None = None
    final_workers: Count | None = None
    confirmed_worker_levels: list[Count] | None = Field(default=None, max_length=16)
    peak_gpu_instances: Count | None = None
    final_gpu_instances: Count | None = None
    gpu0_inference_requests: Count | None = None
    gpu1_inference_requests: Count | None = None
    gpu0_units_completed: Count | None = None
    gpu1_units_completed: Count | None = None
    unattributed_units: Count | None = None
    gpu1_load_seconds: Seconds | None = None
    gpu1_stopped_cleanly: bool | None = None
    scale_out_seconds: Seconds | None = None
    worker_scale_out_seconds: Seconds | None = None
    scale_in_seconds: Seconds | None = None
    telemetry_failed_events: Count | None = None
    reasons: list[Token] | None = Field(default=None, max_length=16)


EVENT_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "notebook_started": _NotebookStartedPayload,
    "environment_ready": _StatusPayload,
    "model_located": _StatusPayload,
    "model_ready": _ModelReadyPayload,
    "model_validation_started": _ModelValidationStartedPayload,
    "worker_validation_completed": _WorkerValidationCompletedPayload,
    "modelcheck_completed": _ModelcheckCompletedPayload,
    "model_validation_completed": _ModelValidationCompletedPayload,
    "scaling_demo_started": _ScalingStartedPayload,
    "worker_scaling_requested": _WorkerScalingRequestedPayload,
    "worker_scaling_completed": _WorkerScalingCompletedPayload,
    "gpu_instance_starting": _GpuStartingPayload,
    "gpu_instance_ready": _GpuReadyPayload,
    "gpu_instance_failed": _GpuFailedPayload,
    "inference_route_changed": _RoutePayload,
    "gpu_instance_draining": _GpuDrainingPayload,
    "gpu_instance_stopped": _GpuStoppedPayload,
    "scaling_controller_error": _ControllerErrorPayload,
    "scaling_demo_completed": _SummaryPayload,
    "scaling_demo_failed": _ScalingFailedPayload,
    "scaling_metrics_snapshot": _SnapshotPayload,
}


class UnknownEventType(ValueError):
    pass


def validate_payload(event_type: str, payload: dict) -> dict:
    model = EVENT_PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise UnknownEventType("unknown event type")
    return model.model_validate(payload).model_dump(exclude_none=True, by_alias=True)


_SAFE_PATH_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
_SAFE_ERROR_TYPE = re.compile(r"^[a-z0-9_]{1,40}$")


def rejection_diagnostics(event_type, exc: Exception, scope: str = "payload", limit: int = 5) -> list[dict]:
    """Event type, field path and error type only; never the submitted values, messages or unknown key names."""
    known = event_type if isinstance(event_type, str) and event_type in EVENT_PAYLOAD_MODELS else "<unknown>"
    if isinstance(exc, UnknownEventType):
        return [{"event_type": known, "field": "event_type", "error_type": "unknown_event_type"}]
    if not isinstance(exc, ValidationError):
        return [{"event_type": known, "field": scope, "error_type": "invalid"}]
    out = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False)[:limit]:
        parts = [p if isinstance(p, int) else (p if _SAFE_PATH_PART.match(p) and not is_unsafe(p) else "<key>") for p in err["loc"]]
        kind = err["type"] if _SAFE_ERROR_TYPE.match(err["type"]) else "invalid"
        out.append({"event_type": known, "field": ".".join([scope, *map(str, parts)]), "error_type": kind})
    return out
