import json

import httpx
import pytest

from workers import model_client, otel


@pytest.fixture
def sdk(monkeypatch):
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    monkeypatch.setenv("AOB_OTEL_ENABLED", "true")
    monkeypatch.setenv("AOB_RUN_ID", "run-0123456789abcdef")
    yield export
    otel.shutdown_otel()


def test_console_model_spans_preserve_results_and_hide_payloads(sdk, monkeypatch, capsys):
    sentinel = "PRIVATE_PAYLOAD"
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"secret={sentinel}")
    client = model_client.LlamaCppClient("http://private.invalid", "Qwen")
    responses = iter([httpx.Response(200, json={"choices": [{"message": {"content": sentinel}}],
                                                "usage": {"prompt_tokens": 25, "completion_tokens": False}}),
                      httpx.Response(500, text=sentinel)])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(responses))
    otel.configure_otel()
    otel.configure_otel()
    with otel.span("aob.run"):
        assert client.generate(sentinel, sentinel, 1) == sentinel
        with pytest.raises(model_client.LLMUnavailable, match=sentinel.lower()):
            client.generate(sentinel, sentinel, 1)
    otel.shutdown_otel()
    output = capsys.readouterr().out
    assert sentinel.lower() not in output.lower() and "private.invalid" not in output
    ok, err, root = map(json.loads, output.splitlines())
    assert ok["name"] == err["name"] == "chat Qwen"
    assert ok["parent_id"] == err["parent_id"] == root["context"]["span_id"]
    assert ok["attributes"] == {"gen_ai.operation.name": "chat", "gen_ai.request.model": "Qwen",
                                "aob.run_id": "run-0123456789abcdef", "gen_ai.usage.input_tokens": 25}
    assert err["status"] == {"status_code": "ERROR"} and not err["events"]


@pytest.mark.parametrize("failure", ["init", "export"])
def test_telemetry_failure_does_not_change_business_outcome(sdk, failure, monkeypatch):
    called = []

    def broken(*args, **kwargs):
        called.append(True)
        raise RuntimeError("unavailable")

    monkeypatch.setattr(sdk.ConsoleSpanExporter, "__init__" if failure == "init" else "export", broken)
    otel.configure_otel()
    with otel.span("work"):
        result = 42
    with pytest.raises(ValueError, match="business failure"), otel.span("failed"):
        raise ValueError("business failure")
    otel.shutdown_otel()  # flush the real batch processor
    assert called and result == 42


def test_disabled_and_unknown_exporter_do_not_initialize(monkeypatch):
    for enabled, exporter in (("false", "console"), ("true", "otlp")):
        monkeypatch.setenv("AOB_OTEL_ENABLED", enabled)
        monkeypatch.setenv("AOB_OTEL_EXPORTER", exporter)
        otel.configure_otel()
        with otel.span("work") as current:
            assert current is None
