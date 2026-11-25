"""Adapter for the single, operator-configured notebook. No remote cancel API."""
from contextlib import closing

import demo_store
import kaggle_launcher


class KaggleAgentRunClient:
    def start_run(self, context: dict) -> dict:
        run_id = context["remote_run_id"]
        with closing(demo_store.connect()) as conn:
            run = demo_store.get_run(conn, run_id)
            if not run or run["status"] != "submitting":
                return {"state": "uncertain", "business_status": "unknown"}
            token = kaggle_launcher.dispatch_deadline.set(context.get("deadline"))
            try:
                kaggle_launcher.submit(run_id, run["mode"] or "light", run["units"],
                                       int(run["event_key_version"] or 1))
            finally:
                kaggle_launcher.dispatch_deadline.reset(token)
            demo_store.advance(conn, run_id, "queued")
        return {"state": "accepted", "business_status": "running"}

    def get_status(self, remote_run_id: str) -> dict:
        import demo_service

        with closing(demo_store.connect()) as conn:
            run = demo_store.get_run(conn, remote_run_id)
            if not run or run["status"] == "submitting":
                # The CLI reports the notebook's latest version, not a per-push ID.
                return {"state": "uncertain", "business_status": "unknown"}
            run = demo_service.sync_run(conn, run)
            if run["status"] in demo_store.ACTIVE:
                return {"state": "accepted", "business_status": "running"}
            if run["error"] and run["error"] != "Kaggle reported an error":
                return {"state": "uncertain", "business_status": "unknown"}
            outcome = demo_service.summarize_run(conn, run)["result"]
            return {"state": "terminal", "business_status":
                    {"passed": "complete", "failed": "failed"}.get(outcome, "unknown")}
