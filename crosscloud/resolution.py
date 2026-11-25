"""Fixed built-in aliases. An operator approves a specific immutable deployment ID."""
import ipaddress
import os
from pathlib import Path
import re

import config
from .azure import AzureAgentRunClient
from .kaggle import KaggleAgentRunClient
from .policy import ServiceError


def approved_target(deployment):
    provider = deployment["provider"]
    if provider not in ("azure", "kaggle"):
        raise ServiceError(409, "NOT_SUPPORTED")
    if (deployment["deployment_id"] != os.getenv(f"AOB_{provider.upper()}_RUN_DEPLOYMENT_ID")
            or deployment["endpoint_ref"] != f"ref:{provider}-agent"
            or deployment["credential_ref"] != f"ref:{provider}-run"):
        raise ServiceError(403, "TARGET_NOT_APPROVED")
    if provider == "kaggle":
        from kaggle_launcher import KAGGLE_NOTEBOOK_REF
        return {"notebook": KAGGLE_NOTEBOOK_REF}
    host = os.getenv("AOB_AZURE_RUN_HOST", "")
    try:
        address = ipaddress.IPv4Address(os.getenv("AOB_AZURE_RUN_IPV4", ""))
    except ValueError:
        raise ServiceError(503, "TARGET_CONFIGURATION") from None
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (not address.is_global or address.is_multicast or address.is_reserved
            or len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", host)):
        raise ServiceError(503, "TARGET_CONFIGURATION")
    return {"hostname": host, "address": str(address)}


def resolve(deployment, target, deadline):
    if approved_target(deployment) != target:
        raise ServiceError(403, "TARGET_CHANGED")
    if deployment["provider"] == "kaggle":
        return KaggleAgentRunClient()
    try:
        path = Path(os.environ["AOB_AZURE_RUN_TOKEN_FILE"])
        with path.open("rb") as f:
            token = f.read(4097).rstrip(b"\r\n").decode("ascii")
        if not re.fullmatch(r"[!-~]{32,4096}", token) or token in config.get_otlp_source_tokens().values():
            raise ValueError
    except (OSError, KeyError, ValueError):
        raise ServiceError(503, "RUN_CREDENTIAL_UNAVAILABLE") from None
    return AzureAgentRunClient(**target, token=token, deadline=deadline)
