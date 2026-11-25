"""Independent structural contracts. No adapters are installed in the control plane."""
from typing import Protocol

from .policy import ServiceError


class DeploymentProvider(Protocol):
    def capabilities(self) -> frozenset[str]: ...
    def inspect(self, resource_ref: str) -> dict: ...
    def provision(self, spec: dict, operation_id: str) -> dict: ...
    def delete(self, resource_ref: str, operation_id: str) -> None: ...


class AgentRunClient(Protocol):
    def start_run(self, context: dict) -> dict: ...
    def get_status(self, remote_run_id: str) -> dict: ...


class CancellableRunClient(AgentRunClient, Protocol):
    def cancel(self, remote_run_id: str) -> dict: ...


class InferenceProvider(Protocol):
    # Structural subset of ModelClient: no changes needed in existing workers.
    def generate(self, system: str, user: str, timeout: float) -> str: ...


class TelemetryProvider(Protocol):
    def get_health(self, resource_ref: str) -> dict: ...
    def get_metrics(self, resource_ref: str, start: float, end: float) -> dict: ...


def check_run_capability(principal, deployment):
    principal.require("aob:runs:execute")
    if deployment["execution_policy"] == "observe_only":
        raise ServiceError(403, "OBSERVE_ONLY")
    if (deployment["runtime_type"] != "agent" or deployment["status"] != "registered"
            or deployment["connection_mode"] != "public_https" or "start" not in deployment["capabilities"]):
        raise ServiceError(409, "NOT_SUPPORTED")
