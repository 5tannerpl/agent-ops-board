"""Optional, process-local console tracing for Kaggle workers."""
import logging
import os
import sys
from contextlib import contextmanager, nullcontext

log = logging.getLogger(__name__)
_provider = _tracer = None


def configure_otel():
    global _provider, _tracer
    if _provider is not None or os.getenv("AOB_OTEL_ENABLED", "").lower() != "true":
        return
    if os.getenv("AOB_OTEL_EXPORTER", "console") != "console":
        log.warning("AOB tracing disabled: only console export is supported")
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        # A private provider avoids replacing or exporting through a host application's provider.
        attributes = {"service.name": os.getenv("AOB_OTEL_SERVICE_NAME", "aob-kaggle-agent"),
                      "deployment.environment.name": os.getenv("AOB_OTEL_ENVIRONMENT", "kaggle-demo"),
                      "aob.source": "kaggle"}
        if commit := os.getenv("AOB_OTEL_COMMIT"):
            attributes["service.version"] = commit[:12]
        _provider = TracerProvider(resource=Resource(attributes), shutdown_on_exit=False)
        _provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter(
            out=sys.stdout, formatter=lambda span: span.to_json(indent=None) + "\n")))
        _tracer = _provider.get_tracer("aob.kaggle")
    except Exception:
        shutdown_otel()
        log.warning("AOB tracing unavailable: SDK initialization failed")
    else:
        log.warning("AOB tracing enabled (console)")


def shutdown_otel():
    global _provider, _tracer
    provider, _provider, _tracer = _provider, None, None
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:
            log.warning("AOB tracing: shutdown failed")


@contextmanager
def _record(name, attributes):
    try:
        if run_id := os.getenv("AOB_RUN_ID"):
            attributes["aob.run_id"] = run_id
        if name == "aob.run" and os.getenv("AOB_SCALING_MODE") in ("light", "high_concurrency"):
            attributes["aob.scaling_mode"] = os.environ["AOB_SCALING_MODE"]
            if (units := os.getenv("AOB_UNITS", "")).isascii() and units.isdecimal():
                attributes["aob.units"] = int(units)
        manager = _tracer.start_as_current_span(name, attributes=attributes, record_exception=False,
                                                set_status_on_exception=False)
        current = manager.__enter__()
    except Exception:
        log.warning("AOB tracing: could not start span")
        yield None
        return
    try:
        yield current
    except BaseException:
        # SDK exception events/descriptions may contain the HTTP body or model output.
        try:
            from opentelemetry.trace import StatusCode
            current.set_status(StatusCode.ERROR)
        except Exception:
            log.warning("AOB tracing: could not mark span failed")
        raise
    finally:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            log.warning("AOB tracing: could not end span")


def span(name, attributes=None):
    return nullcontext(None) if _tracer is None else _record(name, dict(attributes or {}))


def record_usage(current, usage):
    if current is None or not isinstance(usage, dict):
        return
    for key, attribute in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        count = usage.get(key)
        if type(count) is int and count >= 0:
            try:
                current.set_attribute(f"gen_ai.usage.{attribute}", count)
            except Exception:
                log.warning("AOB tracing: could not record token count")
