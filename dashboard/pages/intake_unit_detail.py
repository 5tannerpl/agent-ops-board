"""Intake Unit Detail (page C): source text -> intent label -> verbatim evidence, plus workflow and handoff."""
import html

import pandas as pd
import streamlit as st

import api_client
import highlight
import ui

ui.page_header("Intake Unit Detail")

unit_id = st.query_params.get("unit_id") or st.session_state.get("unit_id")
ui.back_to_dashboard()
if not unit_id:
    unit_id = st.text_input("Unit ID", placeholder="INT-0001")
    if not unit_id:
        st.stop()
st.session_state["unit_id"] = unit_id
st.query_params["unit_id"] = unit_id

first = ui.api_guard(api_client.intake_unit, unit_id)
live = first["status"] in ("PENDING", "RUNNING", "WAITING_RETRY")


@st.fragment(run_every=2 if live else None)
def detail() -> None:
    try:
        u = api_client.intake_unit(unit_id)
        steps = api_client.intake_steps(unit_id)
    except api_client.ApiError as exc:
        st.error(str(exc))
        return
    if live and u["status"] not in ("PENDING", "RUNNING", "WAITING_RETRY"):
        st.rerun()

    h = st.columns(6)
    ui.stat(h[0], "Unit ID", u["unit_id"])
    ui.stat(h[1], "Channel", u["channel"])
    ui.stat(h[2], "Conversation", u["conversation_id"])
    ui.stat(h[3], "Received", ui.fmt_ts(u["received_at"]))
    ui.stat(h[4], "Status", ui.badge(u["status"]))
    ui.stat(h[5], "Attempt", f"{u['attempt']} / {u['max_attempts']}")
    st.caption("Source message refs: " + ", ".join(u["source_message_ids"]))
    if u["assigned_worker"] and st.button(f"Assigned worker: {u['assigned_worker']} →", key="open_worker"):
        ui.open_worker(u["assigned_worker"])
    if u["error_code"]:
        st.error(f"`{u['error_code']}` — {u['error_message'] or ''}")

    # ---- intents + evidence ---------------------------------------------
    labels = [i["label"] for i in u["intents"]]
    colors = highlight.label_colors(labels)
    st.subheader("Intents and evidence")
    selected = "All"
    if labels:
        selected = st.pills("Highlight intent", ["All", *labels], default="All", key="intent_pick") or "All"
    elif u["status"] == "COMPLETED":
        st.info("No intents were recognised in this unit.")
    else:
        st.caption("Intents appear after the model result is validated and saved.")

    spans_by_seg: dict[str, list] = {}
    texts = {s["id"]: s["text"] for s in u["segments"]}
    rows = []
    for intent in u["intents"]:
        for ev in intent["evidence"]:
            sid = ev["source_message_id"]
            spans = highlight.valid_spans(texts.get(sid, ""), [ev], intent["label"])
            if spans and selected in ("All", intent["label"]):
                spans_by_seg.setdefault(sid, []).extend(spans)
            rows.append({"Intent": intent["label"], "Verbatim evidence": ev["quote"] or "—",
                         "Source": sid or "—",
                         "Offsets": f"{ev['start_offset']}–{ev['end_offset']}" if spans else "—",
                         "Check": "✅ verified" if spans else "⚠️ Unverified evidence"})
    if labels:
        legend = " ".join(f'<span style="background:{colors[l]};padding:1px 6px;border-radius:3px;'
                          f'margin-right:6px">{html.escape(l)}</span>' for l in labels)
        st.markdown(legend, unsafe_allow_html=True)

    st.subheader("Original input")
    if u["source_error"]:
        st.error(f"Source content unavailable: {u['source_error']}")
    for seg in u["segments"]:
        who = f"{seg['speaker']} · " if seg["speaker"] and u["channel"] != "email" else ""
        stamp = f" · {seg['timestamp']}" if seg["timestamp"] not in (None, "") else ""
        kind = "Subject" if seg["kind"] == "subject" else ("Body" if seg["kind"] == "body" else "")
        st.markdown(f"<small>{html.escape(kind + (' ' if kind else '') + who)}{html.escape(seg['id'])}"
                    f"{html.escape(stamp)}</small>", unsafe_allow_html=True)
        body = highlight.highlight_html(seg["text"], spans_by_seg.get(seg["id"], []), colors)
        st.markdown(f'<div style="white-space:pre-wrap;border-left:3px solid #ccc;padding:4px 10px;'
                    f'margin-bottom:8px">{body}</div>', unsafe_allow_html=True)
    for att in u["attachments"]:
        st.markdown(f"📎 `{att['filename']}` ({att['size_bytes']} bytes, synthetic) — "
                    f"[download]({api_client.API_URL}/v1/intake-units/{u['unit_id']}/attachments/{att['attachment_id']})")

    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("Customer statements are claims. Verification belongs to downstream agents.")

    # ---- entities, validation -------------------------------------------
    st.subheader("Extracted entities")
    if u["entities"]:
        st.dataframe(pd.DataFrame([{
            "Type": e["type"], "Value": f"{e['value']} {e['currency'] or ''}".strip(),
            "Source span": f"{e['source_message_id']} [{e['start_offset']}–{e['end_offset']}] “{e['quote']}”"
            if e["span_verified"] else f"Unverified span “{e['quote']}”",
            "Verification status": "UNVERIFIED (customer claim)"} for e in u["entities"]]),
            hide_index=True, width="stretch")
    else:
        st.caption("None.")
    v = u["validation"]
    st.subheader("Output validation")
    if v:
        c = st.columns(4)
        ui.stat(c[0], "Schema", "valid" if v["schema_ok"] else "invalid")
        ui.stat(c[1], "Parse failures", v.get("parse_failures", 0))
        ui.stat(c[2], "Repair attempts", v.get("repair_attempts", 0))
        ui.stat(c[3], "Model", u["result_meta"]["model_version"])
        for w in v["warnings"]:
            st.warning(w)
    else:
        st.caption("No validated result yet.")

    # ---- workflow timeline ----------------------------------------------
    st.subheader("Workflow")
    attempts = steps["attempts"]
    if attempts:
        latest = attempts[-1]["attempt"]
        pick = st.selectbox("Attempt", [a["attempt"] for a in attempts], index=len(attempts) - 1,
                            key="attempt_pick") if len(attempts) > 1 else latest
        chosen = next(a for a in attempts if a["attempt"] == pick)
        cols = st.columns(len(chosen["steps"]))
        for col, s in zip(cols, chosen["steps"]):
            col.markdown(f"**{s['step_name']}**  \n{ui.badge(s['status'])}  \n{ui.fmt_dur(s['duration_seconds'])}"
                         + (f"  \n`{s['error_code']}`" if s["error_code"] else ""))
    with st.expander(f"Audit events ({len(steps['events'])})"):
        st.dataframe(pd.DataFrame([{
            "Time": ui.fmt_ts(e["timestamp"]), "Worker": e["worker_id"], "Step": e["step_name"] or "—",
            "Event": e["event_type"], "Attempt": e["attempt"], "Error": e["error_code"] or "",
            "Message": e["message"] or ""} for e in steps["events"]]), hide_index=True, width="stretch")

    # ---- handoff ----------------------------------------------------------
    st.subheader("Mock downstream handoff")
    ho = u["handoff"]
    if ho:
        c = st.columns(4)
        ui.stat(c[0], "Status", ui.badge(ho["status"]))
        ui.stat(c[1], "Event ID", ho["handoff_event_id"])
        ui.stat(c[2], "Proposed tasks", ", ".join(ho["targets"]))
        ui.stat(c[3], "Delivered", ui.fmt_ts(ho["delivered_at"]))
        if ho["last_error"]:
            st.caption(f"Last delivery error: {ho['last_error']} (attempts: {ho['attempts']})")
        st.caption(ho["note"])
    else:
        st.caption("No handoff event yet.")


detail()
