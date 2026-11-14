import json

import highlight
from intake import schema

TEXT = "I already paid $2,000. I dispute the balance and request monthly instalments."
SEGS = [{"id": "MSG-1", "text": TEXT, "speaker": "Customer"}]


def test_offsets_are_computed_from_source_not_trusted_from_model():
    raw = json.dumps({"intents": [{"label": "DEBT_DISPUTE", "evidence": [
        {"message_id": "MSG-1", "quote": "I dispute the balance", "start_offset": 999, "end_offset": 1000}]}]})
    ev = schema.validate_output(raw, SEGS)["intents"][0]["evidence"][0]
    assert (ev["start_offset"], ev["end_offset"], ev["verified"]) == (23, 44, True)
    assert TEXT[ev["start_offset"]:ev["end_offset"]] == ev["quote"]


def test_fabricated_quote_is_unverified_without_offsets():
    raw = json.dumps({"intents": [{"label": "COMPLAINT", "evidence": [
        {"message_id": "MSG-1", "quote": "this text is not in the source"}]}]})
    ev = schema.validate_output(raw, SEGS)["intents"][0]["evidence"][0]
    assert ev["verified"] is False and ev["start_offset"] is None


def test_invalid_output_and_unknown_labels():
    for bad in ("not json", '{"intents": "x"}', "[]"):
        try:
            schema.validate_output(bad, SEGS)
        except schema.InvalidOutput:
            continue
        raise AssertionError(bad)
    out = schema.validate_output('```json\n{"intents":[{"label":"BOGUS","evidence":[]}]}\n```', SEGS)
    assert out["intents"] == [] and out["validation"]["warnings"]


def test_highlight_escapes_html_and_handles_overlap():
    text = "<script>x</script> pay now"
    html = highlight.highlight_html(text, [(0, 8, "A"), (4, 12, "B")], {"A": "#111", "B": "#222"})
    assert "<script" not in html and "</script" not in html and "&lt;" in html
    assert 'title="A, B"' in html  # overlapping region carries both labels


def test_valid_spans_rejects_stale_offsets():
    ev = {"verified": True, "start_offset": 0, "end_offset": 4, "quote": "nope"}
    assert highlight.valid_spans("text here", [ev], "A") == []


def test_reasoning_block_is_ignored():
    raw = '<think>maybe {"intents": 1} hmm</think>{"intents": []}'
    assert schema.validate_output(raw, SEGS)["intents"] == []
