"""Replaceable model-client interface: Ollama, llama.cpp server, and a dev-only mock."""
import json
import re
import time
from typing import Protocol

import httpx

import config
from contextvars import ContextVar
from contextlib import contextmanager

_current_unit_id = ContextVar("aob_current_unit_id", default=None)

@contextmanager
def unit_context(unit_id: str):
    token = _current_unit_id.set(unit_id)
    try:
        yield
    finally:
        _current_unit_id.reset(token)


class LLMError(Exception):
    code = "LLM_ERROR"


class LLMTimeout(LLMError):
    code = "LLM_TIMEOUT"


class LLMUnavailable(LLMError):
    code = "LLM_UNAVAILABLE"


class ResourceExhausted(LLMError):
    code = "RESOURCE_EXHAUSTED"


class ModelClient(Protocol):
    backend: str
    model: str
    simulated: bool

    def generate(self, system: str, user: str, timeout: float) -> str: ...
    def server_version(self) -> str | None: ...


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    body = resp.text[:300].lower()
    if any(k in body for k in ("out of memory", "oom", "cuda error", "failed to allocate")):
        raise ResourceExhausted(body)
    raise LLMUnavailable(f"HTTP {resp.status_code}: {body}")


class _HttpClient:
    simulated = False

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        try:
            unit_id = _current_unit_id.get()

            headers = {}
            if unit_id:
                headers["X-AOB-Unit-ID"] = str(unit_id)

            resp = httpx.post(
                self.base_url + path,
                json=body,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeout(str(exc) or "timeout") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailable(type(exc).__name__) from exc
        _raise_for(resp)
        return resp.json()


class OllamaClient(_HttpClient):
    backend = "ollama"

    def generate(self, system: str, user: str, timeout: float) -> str:
        data = self._post("/api/chat", {
            "model": self.model, "stream": False, "format": "json", "keep_alive": "30m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0, "num_predict": 400, "num_ctx": 2048}}, timeout)
        return data["message"]["content"]

    def server_version(self) -> str | None:
        try:
            return "ollama " + httpx.get(self.base_url + "/api/version", timeout=3).json().get("version", "?")
        except Exception:
            return None


class LlamaCppClient(_HttpClient):
    backend = "llamacpp"

    def generate(self, system: str, user: str, timeout: float) -> str:
        data = self._post("/v1/chat/completions", {
            "model": self.model, "temperature": 0, "max_tokens": 600,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout)
        return data["choices"][0]["message"]["content"]

    def server_version(self) -> str | None:
        try:
            return "llama.cpp " + str(httpx.get(self.base_url + "/props", timeout=3).json().get("build_info", "?"))
        except Exception:
            return None


# Keyword rules for the mock. The quote is the matched text itself, so it is always verbatim.
_MOCK_RULES = [
    ("PAYMENT_VERIFICATION", r"I (?:already )?paid \$[\d,]+|I made a payment of \$[\d,]+|I transferred \$[\d,]+"),
    ("DEBT_DISPUTE", r"I dispute the balance|I do not owe this amount|this debt is not mine"),
    ("PAYMENT_ARRANGEMENT", r"request \w+ instalments|pay it off \w+|set up a \w+ payment plan"),
    ("HARDSHIP_NOTIFICATION", r"I lost my job|financial hardship"),
    ("ACCOUNT_BALANCE_REQUEST", r"statement of my current balance|how much do I owe"),
    ("COMPLAINT", r"lodge a complaint"),
    ("CEASE_CONTACT_REQUEST", r"stop calling me|do not contact me"),
    ("DETAILS_UPDATE", r"my new address is[^.]*|update my phone number"),
]


class MockClient:
    """Rule-based stand-in for development and tests. NOT a model: results must never be
    presented as inference throughput."""
    backend = "mock"
    model = "mock-rules"
    simulated = True

    def generate(self, system: str, user: str, timeout: float) -> str:
        if config.MOCK_LATENCY:
            time.sleep(config.MOCK_LATENCY)
        segments, _ = json.JSONDecoder().raw_decode(user.split("SEGMENTS:", 1)[1])
        intents: dict[str, list] = {}
        entities = []
        for seg in segments:
            if seg["speaker"].lower() == "agent":
                continue
            for label, pattern in _MOCK_RULES:
                for m in re.finditer(pattern, seg["text"], re.I):
                    intents.setdefault(label, []).append({"message_id": seg["message_id"], "quote": m.group(0)})
                    amt = re.search(r"\$([\d,]+)", m.group(0))
                    if amt and label == "PAYMENT_VERIFICATION":
                        entities.append({"type": "REPORTED_PAYMENT", "value": amt.group(1), "currency": "AUD",
                                         "message_id": seg["message_id"], "quote": m.group(0)})
        return json.dumps({"intents": [{"label": k, "evidence": v} for k, v in intents.items()],
                           "entities": entities})

    def server_version(self) -> str | None:
        return None


def make_client() -> ModelClient:
    backend = config.MODEL_BACKEND
    if backend == "mock":
        return MockClient()
    if backend == "ollama":
        return OllamaClient(config.MODEL_URL, config.MODEL_NAME)
    if backend == "llamacpp":
        return LlamaCppClient(config.MODEL_URL, config.MODEL_NAME)
    raise ValueError(f"unknown AOB_MODEL_BACKEND {backend!r}")
