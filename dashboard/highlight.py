"""Evidence highlighting. All source text is HTML-escaped; only <mark> wrappers are added."""
import html

PALETTE = ["#ffe58f", "#b7eb8f", "#91d5ff", "#ffadd2", "#d3adf7", "#ffd591", "#87e8de", "#ffccc7", "#d9d9d9"]


def valid_spans(text: str, evidence: list[dict], key: str) -> list[tuple[int, int, str]]:
    """Keep only evidence whose stored offsets still match the verbatim quote in the source text."""
    spans = []
    for ev in evidence:
        s, e = ev.get("start_offset"), ev.get("end_offset")
        if (ev.get("verified") and isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= len(text)
                and text[s:e] == ev.get("quote")):
            spans.append((s, e, key))
    return spans


def highlight_html(text: str, spans: list[tuple[int, int, str]], colors: dict[str, str]) -> str:
    """Render text with spans highlighted; overlapping spans are split into segments that carry every
    covering label (striped background + tooltip)."""
    points = sorted({0, len(text), *[p for s, e, _ in spans for p in (s, e)]})
    out = []
    for a, b in zip(points, points[1:]):
        chunk = html.escape(text[a:b])
        keys = []
        for s, e, k in spans:
            if s <= a and b <= e and k not in keys:
                keys.append(k)
        if not keys:
            out.append(chunk)
        elif len(keys) == 1:
            out.append(f'<mark style="background:{colors[keys[0]]};padding:0 2px;border-radius:3px" '
                       f'title="{html.escape(keys[0])}">{chunk}</mark>')
        else:
            stops = ", ".join(f"{colors[k]} {i * 100 // len(keys)}% {(i + 1) * 100 // len(keys)}%"
                              for i, k in enumerate(keys))
            out.append(f'<mark style="background:linear-gradient(90deg, {stops});padding:0 2px;'
                       f'border-radius:3px;border-bottom:2px solid #555" '
                       f'title="{html.escape(", ".join(keys))}">{chunk}</mark>')
    return "".join(out)


def label_colors(labels: list[str]) -> dict[str, str]:
    return {label: PALETTE[i % len(PALETTE)] for i, label in enumerate(labels)}
