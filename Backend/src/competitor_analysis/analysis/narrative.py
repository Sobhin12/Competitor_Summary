"""
LLM-written executive narrative over a period's rule-based insights.

The model receives the structured insights (headlines, drivers, context,
implications, caveats, data checks) and writes the "so what" across them:
themes spanning companies, competitive implications, what to watch. It adds
no figures - every number is verified against the insight payload, and the
UI presents this as interpretation, separate from the computed cards.

Generated once per (period, source-data fingerprint, model) and cached on
disk, so opening the Highlights page doesn't cost a call each time.
"""
import hashlib
import json
import os
import threading

from competitor_analysis import paths
from competitor_analysis.analysis import insights, llm, store as S, verify

CACHE_DIR = paths.CACHE_DIR / "narrative"
_lock = threading.Lock()

SYSTEM = """You are a senior competitive-intelligence analyst covering India's standalone health insurers (SAHI).
You receive computed insights for one reporting period: each has a headline, the drivers that explain it,
context and a rule-based implication. Write the executive narrative a strategy team would read first.

Write GitHub markdown with exactly these sections:
### Executive summary
3-5 bullets: the most important developments across the market, not one bullet per company.
### Themes across insurers
2-4 bullets connecting insights (e.g. who is buying growth at the cost of profitability, channel or segment
strategies diverging, capital pressure linked to growth).
### What to watch next quarter
2-3 bullets of forward-looking questions the data raises.

Rules:
- Use ONLY numbers that appear in the input, written exactly as displayed there. Do not compute new numbers.
- Stay neutral across companies; no company is "us".
- Interpretation is welcome but must be phrased as such ("suggests", "likely", "points to").
- Respect caveats: treat early-stage insurers and figures flagged in data_checks with caution, and say so if
  you rely on them.
- Be concise: no preamble, no conclusion paragraph."""


def _payload(report):
    keep = ("theme", "entity_name", "headline", "implication", "context", "caveats")
    items = []
    for i in report["insights"]:
        d = {k: i[k] for k in keep if i.get(k)}
        d["drivers"] = [f"{x['factor']}: {x['display']}" for x in i["drivers"]]
        d["evidence"] = [f"{e['label']} ({e['entity']}): {e['display']} vs {e['prior_display']} "
                         f"({e['change_display']})" for e in i["evidence"]]
        items.append(d)
    return {"period": report["period_label"], "compared_with": report["prior_period_label"], "insights": items,
            "data_checks": [c["message"] for c in report["data_checks"]][:6]}


def _cache_path(period, payload, model):
    key = hashlib.md5(json.dumps([payload, model, SYSTEM], sort_keys=True).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{period}_{key}.json"


def build(store, period=None, refresh=False, client=None):
    report = insights.build(store, period)
    payload = _payload(report)
    client = client or llm.get_client()
    path = _cache_path(report["period"], payload, f"{client.name}:{client.model}")
    if not refresh and path.exists():
        return json.loads(path.read_text(encoding="utf-8")) | {"cached": True}

    with _lock:
        turn = client.complete(SYSTEM, [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], [])
    text = turn.text.strip()
    check = verify.verify(text, [payload])
    out = {"period": report["period"], "period_label": S.period_label(report["period"]), "markdown": text,
           "unverified": check["unverified"], "checked": len(check["numbers"]),
           "provider": client.name, "model": client.model, "cached": False}
    os.makedirs(CACHE_DIR, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
