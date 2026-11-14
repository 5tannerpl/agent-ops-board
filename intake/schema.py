"""Intent taxonomy, prompt construction and strict validation of model output."""
import json
import re

import config

INTENT_LABELS = {
    "PAYMENT_VERIFICATION": "Customer says they already made a payment.",
    "DEBT_DISPUTE": "Customer disputes the debt, balance or ownership of the account.",
    "PAYMENT_ARRANGEMENT": "Customer asks for instalments or a payment plan.",
    "HARDSHIP_NOTIFICATION": "Customer reports financial hardship or a change in circumstances.",
    "ACCOUNT_BALANCE_REQUEST": "Customer asks for a balance or statement.",
    "COMPLAINT": "Customer complains about service or conduct.",
    "CEASE_CONTACT_REQUEST": "Customer asks not to be contacted or to change contact method.",
    "DETAILS_UPDATE": "Customer provides updated contact or personal details.",
    "OTHER": "A request that fits none of the other labels.",
}
ENTITY_TYPES = ["REPORTED_PAYMENT", "DISPUTED_AMOUNT", "PROPOSED_INSTALMENT", "REPORTED_DATE", "OTHER"]

SYSTEM_PROMPT = (
    "You are an intake agent for a collections contact centre. You identify WHAT THE CUSTOMER SAID. "
    "You never verify facts, never decide actions and never write replies. "
    "Reply with a single JSON object and nothing else."
)


class InvalidOutput(Exception):
    """Model output could not be parsed or does not match the schema."""


def build_user_prompt(segments: list[dict], channel: str) -> str:
    labels = "\n".join(f"- {k}: {v}" for k, v in INTENT_LABELS.items())
    seg_json = json.dumps(
        [{"message_id": s["id"], "speaker": s.get("speaker", ""), "text": s["text"]} for s in segments],
        ensure_ascii=False,
    )
    return (
        f"Channel: {channel}\n"
        f"Allowed intent labels:\n{labels}\n"
        f"Allowed entity types: {', '.join(ENTITY_TYPES)}\n\n"
        "Task: list every intent the CUSTOMER expresses (agent/staff text is context only). A message "
        "may contain several intents. For each intent give one or more evidence quotes copied "
        "VERBATIM and character-for-character from the segment text (short phrases, not whole "
        "paragraphs). Also list entities (amounts, dates) with a verbatim quote. Customer statements "
        "are claims, not verified facts.\n"
        'Output format: {"intents":[{"label":"LABEL","evidence":[{"message_id":"ID","quote":"..."}]}],'
        '"entities":[{"type":"TYPE","value":"123.45","currency":"AUD","message_id":"ID","quote":"..."}]}\n\n'
        f"SEGMENTS:{seg_json}"
    )


def build_repair_prompt(original_prompt: str, previous: str, error: str) -> str:
    return (
        f"{original_prompt}\n\nYour previous reply was invalid ({error}). Previous reply:\n"
        f"{previous[:1500]}\n\nReply again with ONLY the corrected JSON object."
    )


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()  # reasoning models (DeepSeek-R1)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise InvalidOutput("no JSON object in output")
        try:
            data = json.loads(text[start : end + 1])
        except ValueError as exc:
            raise InvalidOutput("malformed JSON") from exc
    if not isinstance(data, dict):
        raise InvalidOutput("top-level JSON is not an object")
    return data


def locate_quote(segments: list[dict], quote: str, message_id: str | None) -> tuple[str, int | None, int | None, bool]:
    """Verify a quote against stored source text; offsets are computed here, never trusted."""
    by_id = {s["id"]: s for s in segments}
    quote = quote if isinstance(quote, str) else ""
    if quote:
        if message_id in by_id:
            idx = by_id[message_id]["text"].find(quote)
            if idx >= 0:
                return message_id, idx, idx + len(quote), True
        else:
            for seg in segments:
                idx = seg["text"].find(quote)
                if idx >= 0:
                    return seg["id"], idx, idx + len(quote), True
    return (message_id if isinstance(message_id, str) else ""), None, None, False


def _num(value):
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            return float(cleaned) if "." in cleaned else int(cleaned)
        except ValueError:
            return value
    return value


def validate_output(raw: str, segments: list[dict]) -> dict:
    """Parse model output into the stored result structure; raises InvalidOutput."""
    data = _extract_json(raw)
    if not isinstance(data.get("intents"), list):
        raise InvalidOutput("'intents' must be a list")
    warnings: list[str] = []
    intents = []
    for item in data["intents"]:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            raise InvalidOutput("intent without label")
        label = item["label"].strip().upper()
        if label not in INTENT_LABELS:
            warnings.append(f"dropped unknown label {label[:40]!r}")
            continue
        evidence = []
        ev_list = item.get("evidence", [])
        if not isinstance(ev_list, list):
            raise InvalidOutput("'evidence' must be a list")
        for ev in ev_list:
            if not isinstance(ev, dict):
                raise InvalidOutput("evidence item is not an object")
            mid = ev.get("source_message_id", ev.get("message_id"))
            quote = ev.get("quote")
            sid, start, end, ok = locate_quote(segments, quote, mid)
            evidence.append(
                {"source_message_id": sid, "quote": quote if isinstance(quote, str) else "",
                 "start_offset": start, "end_offset": end, "verified": ok}
            )
            if not ok:
                warnings.append(f"unverified evidence for {label}")
        existing = next((i for i in intents if i["label"] == label), None)
        if existing:
            existing["evidence"].extend(evidence)
        else:
            intents.append({"label": label, "evidence": evidence})
    entities = []
    ents = data.get("entities", [])
    if ents is None:
        ents = []
    if not isinstance(ents, list):
        raise InvalidOutput("'entities' must be a list")
    for ent in ents:
        if not isinstance(ent, dict):
            raise InvalidOutput("entity is not an object")
        etype = str(ent.get("type", "OTHER")).upper()
        etype = etype if etype in ENTITY_TYPES else "OTHER"
        mid = ent.get("source_message_id", ent.get("message_id"))
        sid, start, end, ok = locate_quote(segments, ent.get("quote"), mid)
        entities.append(
            {"type": etype, "value": _num(ent.get("value")), "currency": ent.get("currency"),
             "source_message_id": sid, "quote": ent.get("quote") if isinstance(ent.get("quote"), str) else "",
             "start_offset": start, "end_offset": end, "span_verified": ok,
             # Entities are customer claims; verification belongs to downstream agents.
             "verified": False}
        )
    return {"schema_version": config.SCHEMA_VERSION, "intents": intents, "entities": entities,
            "validation": {"schema_ok": True, "warnings": warnings}}
