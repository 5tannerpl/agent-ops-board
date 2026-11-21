import ast
import json

import kaggle_launcher
from tests.test_demo_api import demo


def _console_span(index, name, parent_id=None, unit_id=None):
    attributes = {"aob.run_id": "run-0123456789abcdef"}
    if unit_id:
        attributes.update({"aob.unit_id": unit_id, "aob.worker_id": "worker-1", "aob.attempt": 1})
    if name.startswith("chat "):
        attributes.update({
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "test-model",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 4,
        })
    return {
        "context": {"trace_id": f"0x{index:032x}", "span_id": f"0x{index + 1000:016x}"},
        "parent_id": f"0x{parent_id:016x}" if parent_id else None,
        "name": name,
        "start_time": 1_700_000_000_000_000_000 + index,
        "end_time": 1_700_000_001_000_000_000 + index,
        "status": {"status_code": "UNSET"},
        "attributes": attributes,
        "resource": {"attributes": {"service.name": "aob-kaggle-worker"}},
    }


def test_console_exporter_structure_normalizes_308_spans_and_batches():
    raw = []
    for index in range(8):
        raw.append(_console_span(index + 1, "aob.run"))
    for index in range(100):
        task_span = index + 100
        raw.append(_console_span(task_span, "aob.worker.task", unit_id=f"unit-{index:03d}"))
        raw.append(_console_span(task_span + 1000, "chat test-model", task_span, f"unit-{index:03d}"))
        raw.append(_console_span(task_span + 2000, "aob.result.validation", task_span, f"unit-{index:03d}"))

    spans = [kaggle_launcher.normalize_console_span(item, "run-0123456789abcdef") for item in raw]
    spans = [span for span in spans if span is not None]
    assert len(spans) == 308
    assert len([span for span in spans if span["name"].startswith("chat ")]) == 100
    assert [len(spans[offset:offset + 200]) for offset in range(0, len(spans), 200)] == [200, 108]
    assert all(not value.startswith("0x") for span in spans for value in (span["trace_id"], span["span_id"])
               if isinstance(value, str))
    llm = next(span for span in spans if span["name"].startswith("chat "))
    assert llm["attributes"]["aob.unit_id"] == "unit-000"
    assert llm["attributes"]["aob.worker_id"] == "worker-1"
    assert llm["attributes"]["aob.attempt"] == 1
    assert llm["parent_id"]


def test_generated_uploader_uses_context_ids_and_reports_upload_outcomes():
    source = kaggle_launcher.build_trace_upload_cell()
    ast.parse(source)
    assert "context.get(\"trace_id\")" in source
    assert "context.get(\"span_id\")" in source
    assert '.removeprefix("0x")' in source
    assert "no valid spans found" in source
    assert "unexpected HTTP status" in source
    assert "result.get(\"accepted\"" in source
    assert "result.get(\"duplicates\"" in source
    assert "TRACE UPLOAD: complete" in source


def test_restaging_removes_previous_trace_upload_marker(demo, tmp_path, monkeypatch):
    from tests.test_demo_scaling import LEGACY_STEP6
    cells = [{"cell_type": "code", "metadata": {}, "outputs": [],
              "source": LEGACY_STEP6.splitlines(True)}]
    monkeypatch.setattr(kaggle_launcher, "_cli_checked",
                        lambda args, timeout, operation: _fake_pull(tmp_path, cells))
    monkeypatch.setenv("AOB_OTEL_ENABLED", "false")
    kaggle_launcher.stage_notebook("run-0123456789abcdef", tmp_path, "high_concurrency", 100)
    first = json.loads((tmp_path / "nb.ipynb").read_text())["cells"]
    first.append({"cell_type": "code", "metadata": {}, "outputs": [],
                  "source": kaggle_launcher.build_trace_upload_cell().splitlines(True)})
    (tmp_path / "nb.ipynb").write_text(json.dumps({
        "nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": first,
    }))
    assert sum("".join(cell["source"]).startswith(kaggle_launcher.TRACE_UPLOAD_CELL_MARKER)
               for cell in first) == 1
    monkeypatch.setattr(kaggle_launcher, "_cli_checked",
                        lambda args, timeout, operation: _fake_pull(tmp_path, first))
    kaggle_launcher.stage_notebook("run-fedcba9876543210", tmp_path, "high_concurrency", 100)
    second = json.loads((tmp_path / "nb.ipynb").read_text())["cells"]
    assert sum("".join(cell["source"]).startswith(kaggle_launcher.TRACE_UPLOAD_CELL_MARKER)
               for cell in second) == 0


def _fake_pull(target, cells):
    target.mkdir(parents=True, exist_ok=True)
    (target / "kernel-metadata.json").write_text(json.dumps({
        "id": "sillypanda5/notebookb061a06458", "code_file": "nb.ipynb",
        "dataset_sources": ["sillypanda5/agent-ops-qwen-model", "sillypanda5/agent-ops-llama-cuda-t4"],
    }))
    (target / "nb.ipynb").write_text(json.dumps({
        "nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": cells,
    }))
