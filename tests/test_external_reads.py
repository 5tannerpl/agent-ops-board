import json

import config
from tests.test_demo_api import auth, demo
from tests.test_otlp_receiver import headers, observation


def test_external_source_grants_and_filtered_pages(demo, monkeypatch):
    monkeypatch.setattr(config, "OTLP_SOURCE_TOKENS", '{"a":"token-a","b":"token-b"}')
    users = {"auth0|alice": {"enabled": True, "external_sources": ["a"]},
             "auth0|bob": {"enabled": True, "external_sources": ["b"]}}
    config.ALLOWED_USERS_FILE.write_text(json.dumps({"allowed_users": users}))
    for source in ("a", "b"):
        for i in (1, 2):
            req = observation(i, source + str(i), trace=f"{i:032x}")
            response = demo.ingress.post("/v1/traces", content=req.SerializeToString(),
                                         headers=headers(token="token-" + source))
            assert response.status_code == 200
            assert response.headers["x-aob-accepted-spans"] == "1"
    base = "/api/external/runs/agent-run-1"
    # Split services: the private reader does not have the ingress credential map.
    monkeypatch.setattr(config, "OTLP_SOURCE_TOKENS", "")
    monkeypatch.setattr(config, "OTLP_READ_SOURCES", '["a","b"]')
    for source, owner, denied in (("a", "alice", "b"), ("b", "bob", "a")):
        credential = auth("auth0|" + owner)
        assert demo.get("/api/external/sources", headers=credential).json()["items"] == [{"source_id": source}]
        for endpoint in ("/api/external/runs", *(base + suffix for suffix in (
                "/summary", "/workers", "/units", "/timeline", "/traces", f"/traces/{1:032x}",
                f"/traces/{1:032x}/timeline", "/units/unit-1/spans"))):
            assert demo.get(endpoint, headers=credential, params={"source_id": denied}).status_code == 404
            assert demo.get(endpoint, headers=headers(token="token-" + source)).status_code == 401
        assert demo.get("/api/external/runs", headers=credential).json()["items"][0]["span_count"] == 2
        traces = demo.get(base + "/traces?limit=1&offset=1", headers=credential).json()["items"]
        assert [t["trace_id"] for t in traces] == [f"{2:032x}"]
        path = base + "/traces/" + traces[0]["trace_id"]
        assert demo.get(path, headers=credential).json()["span_count"] == 1
        rows = demo.get(path + "/timeline?unit_id=unit-1&worker_id=worker-1", headers=credential).json()["items"]
        assert [r["span_name"] for r in rows] == [source + "2"]
        assert demo.get(path + "/timeline?unit_id=absent", headers=credential).json()["items"] == []
