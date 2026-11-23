"""Read-time Schema v1 projection of retained OTLP evidence."""
from collections import Counter, defaultdict


ROLES = {
    "aob.run": "run", "aob.azure.run": "run", "aob.worker": "worker", "aob.azure.worker": "worker",
    "aob.unit.task": "unit_execution", "aob.worker.task": "unit_execution",
    "aob.unit.completed": "unit_outcome", "aob.unit.failed": "unit_outcome",
    "agent.classify": "classify", "aob.agent.classification": "classify",
    "tool.lookup": "lookup", "aob.tool.lookup": "lookup",
    "result.validation": "validate", "aob.result.validation": "validate",
}


def model_call(span):
    return (span["attributes"].get("gen_ai.operation.name") == "chat"
            or span["span_name"].startswith("chat ") and bool(span["span_name"][5:].strip()))


def role(span):
    return ROLES.get(span["span_name"], "model_call" if model_call(span) else "other")


def outcomes(spans):
    attempts = defaultdict(lambda: defaultdict(set))
    for span in spans:
        if not span["unit_id"]:
            continue
        unit = attempts[span["unit_id"]]
        attrs = span["attributes"]
        if type(attrs.get("aob.attempt")) is int and attrs["aob.attempt"] > 0:
            unit.setdefault(attrs["aob.attempt"], set())
        if span["role"] not in ("unit_execution", "unit_outcome"):
            continue
        attempt = attrs.get("aob.attempt")
        records = unit[attempt if type(attempt) is int and attempt > 0 else None]
        if "aob.task.status" in attrs:
            records.add(attrs["aob.task.status"])
        if span["role"] == "unit_outcome":
            records.add("COMPLETE" if span["span_name"] == "aob.unit.completed" else "FAILED")
    result = {}
    for unit, by_attempt in attempts.items():
        numbered = [n for n in by_attempt if n is not None]
        statuses = by_attempt.get(max(numbered) if numbered else None, set())
        if numbered and None in by_attempt:
            statuses = statuses | by_attempt[None]
            if not by_attempt[None] or not by_attempt[max(numbered)]:
                statuses = set()
        result[unit] = next(iter(statuses)) if len(statuses) == 1 else "UNKNOWN"
    return result


def graph(spans, boundaries):
    ids = {(s["trace_id"], s["span_id"]) for s in spans}
    parents = {}
    external = set()
    unresolved = 0
    for s in spans:
        key = s["trace_id"], s["span_id"]
        parent = s["trace_id"], s["parent_span_id"]
        edge = (*key, parent[1])
        if parent[1] is None:
            continue
        if parent in ids:
            parents[key] = parent
        elif edge in boundaries:
            external.add(edge)
        else:
            unresolved += 1
    seen, cyclic = set(), set()
    for key in parents:
        path = {}
        while key in parents and key not in seen and key not in path:
            path[key] = len(path)
            key = parents[key]
        if key in path:
            cyclic.update(list(path)[path[key]:])
        seen.update(path)
    return {"valid_parent_links": len(parents) - len(cyclic), "cyclic_parent_links": len(cyclic),
            "unresolved_parent_links": unresolved, "external_parent_links": len(external),
            "unmatched_boundaries": len(boundaries - external)}


def metrics(spans):
    roles = Counter(s["role"] for s in spans)
    results = Counter(outcomes(spans).values())
    checks = Counter(s["attributes"].get("aob.validation.passed") for s in spans if s["role"] == "validate")
    calls = [s["attributes"] for s in spans if model_call(s)]
    usage = {}
    for direction in ("input", "output"):
        key = f"gen_ai.usage.{direction}_tokens"
        values = [a[key] for a in calls if type(a.get(key)) is int and a[key] >= 0]
        usage[f"{direction}_tokens"] = sum(values) if values else None
        usage[f"{direction}_measured_calls"] = len(values)
    return {
        "completed": results["COMPLETE"], "completed_units": results["COMPLETE"],
        "failed": results["FAILED"], "failed_units": results["FAILED"], "unknown_outcome_units": results["UNKNOWN"],
        "validations": roles["validate"], "validation_passed": checks[True],
        "validation_failed": checks[False], "validation_unknown": checks[None],
        "agent_classifications": roles["classify"], "tool_lookups": roles["lookup"],
        "application_spans": len(spans) - roles["other"] - roles["model_call"],
        "llm_calls": len(calls), "token_usage": usage if calls else "N/A",
    }
