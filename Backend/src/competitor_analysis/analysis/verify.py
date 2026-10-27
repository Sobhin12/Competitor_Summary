"""
Checks that every number an LLM wrote traces back to a tool result.

The agent's tools return raw values (fractions for percentages, crores for
amounts) alongside display strings. A number in the prose is "verified"
when it matches - within the precision it was written at - one of those
values in any of its plausible renderings (as-is, x100 for a percentage,
absolute value for a signed change). Anything else (an LLM-derived ratio, a
recalled figure) is reported as unverified so the UI can flag it.

Same idea as reporting/report_llm._verify_numbers, which depended on a
reporting.validate module that never existed.
"""
import re

# Numbers that are labels, not data: periods, quarters, years.
_LABEL_RE = re.compile(r"\bFY\s?\d{2}(?:-\d{2})?(?:[_ ]Q[1-4])?\b|\bQ[1-4]\b|\b(?:19|20)\d{2}\b|#\d+\b",
                       re.IGNORECASE)
_NUM_RE = re.compile(
    r"(?P<sign>[+\-−–]?)\s?(?P<cur>₹|Rs\.?\s?|INR\s?)?(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s?(?P<unit>%|pp|percentage points?|bps|x\b|times\b|Cr\b|crore|lakh|k\b|bn\b)?",
    re.IGNORECASE)


def _walk(obj, ids=None):
    """Yield (number, fact_ids) for every number in a tool result, carrying
    the nearest enclosing 'id'/'ids' so a matched number can be cited."""
    if isinstance(obj, dict):
        here = obj.get("ids") or ([obj["id"]] if isinstance(obj.get("id"), str) and "|" in obj["id"] else None)
        here = here or ids
        for k, v in obj.items():
            if k in ("id", "ids"):
                continue
            yield from _walk(v, here)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, ids)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        yield float(obj), ids or []
    elif isinstance(obj, str):
        for m in _NUM_RE.finditer(_LABEL_RE.sub(" ", obj)):
            yield float(m.group("num").replace(",", "")), ids or []


def candidates(tool_results):
    out = []
    for res in tool_results:
        for v, ids in _walk(res):
            for c in {v, abs(v), v * 100, abs(v) * 100}:
                out.append((c, ids))
    return out


def _tolerance(num_text):
    decimals = len(num_text.split(".")[1]) if "." in num_text else 0
    return 0.5 * 10 ** -decimals + 1e-9


def verify(text, tool_results, extra_numbers=()):
    """{'numbers': [{text, value, verified, ids}], 'unverified': [...]}."""
    cands = candidates(tool_results) + [(float(x), []) for x in extra_numbers]
    cleaned = _LABEL_RE.sub(lambda m: " " * len(m.group(0)), text)
    found = []
    for m in _NUM_RE.finditer(cleaned):
        raw = m.group("num")
        value = float(raw.replace(",", ""))
        unit = (m.group("unit") or "").lower()
        # Small bare integers are ranks/counts ("3 of 5", "top 2"), not data.
        if not unit and not m.group("cur") and "." not in raw and value <= 10:
            continue
        scale = {"k": 1e3, "bn": 1e9}.get(unit, 1)
        target = value * scale
        tol = _tolerance(raw) * scale
        if unit == "bps":
            target, tol = value / 100, 0.005
        hit = next((ids for c, ids in cands if abs(c - target) <= tol), None)
        found.append({"text": m.group(0).strip(), "value": value, "verified": hit is not None, "ids": hit or []})
    return {"numbers": found, "unverified": [f["text"] for f in found if not f["verified"]]}
