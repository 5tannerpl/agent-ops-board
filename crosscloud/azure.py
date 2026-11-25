"""Blocking AgentRunClient over the existing single-process Azure sample Run API."""
import asyncio
import json
import re
import time

import httpx


RUN_ID = re.compile(r"azure-py-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
TRACE = re.compile(r"00-(?!0{32}-)[0-9a-f]{32}-(?!0{16}-)[0-9a-f]{16}-[0-9a-f]{2}")


def uncertain(code):
    return {"state": "uncertain", "business_status": "unknown", "code": code}


class AzureAgentRunClient:
    def __init__(self, address, hostname, token, deadline, *, transport=None):
        self.address, self.hostname = address, hostname
        self._token, self.deadline, self._transport = token, deadline, transport

    def start_run(self, context):
        return self._call("POST", context["remote_run_id"], traceparent=context["traceparent"])

    def get_status(self, remote_run_id):
        return self._call("GET", remote_run_id)

    def get_units(self, remote_run_id):
        return self._call("GET", remote_run_id, units=True)

    def _call(self, method, run_id, *, units=False, traceparent=None):
        if not RUN_ID.fullmatch(run_id) or (traceparent is not None and not TRACE.fullmatch(traceparent)):
            raise ValueError("invalid dispatch context")
        budget = min(75 if method == "POST" else 10, self.deadline - time.time())
        if budget <= 0:
            return uncertain("DEADLINE_EXPIRED")
        try:
            status, body = asyncio.run(self._request(method, run_id, units, traceparent, budget))
        except (TimeoutError, httpx.TimeoutException):
            return uncertain("REMOTE_TIMEOUT")
        except (httpx.HTTPError, OSError):
            return uncertain("TRANSPORT_ERROR")
        except (ValueError, RecursionError):
            return uncertain("INVALID_RESPONSE")
        if status not in (200, 202):
            capacity = status == 503 and body == {"detail": "Process run history full"}
            if method == "POST" and (status in (401, 403, 409, 413, 422) or capacity):
                return {"state": "terminal", "business_status": "failed", "code": "REMOTE_REJECTED"}
            return uncertain("REMOTE_UNKNOWN" if status == 404 else
                             "REMOTE_TIMEOUT" if status == 504 else "REMOTE_ERROR")
        if units:
            if (not isinstance(body, list) or len(body) != 100
                    or not all(isinstance(u, dict) and isinstance(u.get("unit_id"), str)
                               and isinstance(u.get("worker_id"), str) for u in body)):
                return uncertain("INVALID_RESPONSE")
            return {"distinct_units": len({u["unit_id"] for u in body}),
                    "workers": len({u["worker_id"] for u in body})}
        if not isinstance(body, dict) or body.get("run_id") != run_id or body.get("total") != 100:
            return uncertain("INVALID_RESPONSE")
        if body.get("status") == "RUNNING":
            return {"state": "accepted", "business_status": "running"}
        if body.get("status") == "FAILED":
            return {"state": "terminal", "business_status": "failed"}
        if body.get("status") == "COMPLETE" and body.get("completed") == 100 and body.get("failed") == 0:
            return {"state": "terminal", "business_status": "complete"}
        return uncertain("INVALID_RESPONSE")

    async def _request(self, method, run_id, units, traceparent, budget):
        headers = {"Host": self.hostname, "Authorization": "Bearer " + self._token,
                   "Accept-Encoding": "identity"}
        path, body = "/demo/runs", None
        if method == "POST":
            headers["traceparent"] = traceparent
            body = {"external_run_id": run_id, "task_count": 100, "concurrency": 4}
        else:
            path += "/" + run_id + ("/units" if units else "")
        # Connect only to the pinned numeric IP; verify TLS against the approved hostname.
        async with asyncio.timeout(budget), httpx.AsyncClient(
            trust_env=False, follow_redirects=False, transport=self._transport,
            timeout=httpx.Timeout(budget, connect=min(5, budget)),
        ) as client, client.stream(method, "https://" + self.address + path, headers=headers,
                                   extensions={"sni_hostname": self.hostname}, json=body) as response:
            if response.status_code not in (200, 202, 503):
                return response.status_code, None
            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > 65536:
                    raise ValueError("response limit")
                data.extend(chunk)
            return response.status_code, json.loads(data)
