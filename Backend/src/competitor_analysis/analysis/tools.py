"""
Deterministic analysis functions over the FactStore. These do every bit of
arithmetic in v2 - the API serves them directly and the chat agent calls
them as tools, so an LLM never computes or recalls a figure itself.

Every function returns a JSON-able dict. Values are raw numbers in the
metric's unit (fractions for pct); each carries a `display` string and the
fact `id`(s) it came from, which the UI turns into citations and the
verifier uses to check the agent's prose.
"""
import statistics

from competitor_analysis.analysis import metrics as M
from competitor_analysis.analysis import store as S

COMPANIES = M.COMPANY_KEYS
BENCHMARKS = ["SAHI", "Industry"]


class ToolError(ValueError):
    """A bad argument the caller (often the LLM) can correct - surfaced as a
    message, not a 500."""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt(value, unit):
    if value is None:
        return "n/a"
    if unit == "pct":
        return f"{value * 100:.1f}%"
    if unit == "cr":
        return f"₹{value:,.0f} Cr" if abs(value) >= 100 else f"₹{value:,.2f} Cr"
    if unit == "lakh":
        return f"₹{value:,.2f} lakh"
    if unit == "inr":
        return f"₹{value:,.0f}"
    if unit == "x":
        return f"{value:.2f}x"
    if unit == "count":
        return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}"
    return f"{value:,.4g}"


def change(cur, prior, unit):
    """{abs, rel, kind, display} for cur vs prior. Ratios (pct/x) move in
    points; amounts/counts in % growth (only defined over a positive base)."""
    if cur is None or prior is None:
        return None
    delta = cur - prior
    if unit == "pct":
        return {"abs": delta, "rel": None, "kind": "pp", "display": f"{delta * 100:+.1f} pp"}
    if unit == "x":
        return {"abs": delta, "rel": None, "kind": "x", "display": f"{delta:+.2f}x"}
    rel = (cur / prior - 1) if prior > 0 else None
    disp = f"{rel * 100:+.1f}%" if rel is not None else f"{'+' if delta >= 0 else '-'}{fmt(abs(delta), unit)}"
    return {"abs": delta, "rel": rel, "kind": "growth", "display": disp}


def suspect_jump(cur, prior, unit):
    """A year-on-year ratio so extreme it is almost certainly a units or
    extraction glitch (e.g. an average claim size filed in rupees one year
    and lakhs the next) rather than a business move."""
    if cur is None or prior is None or unit in ("pct", "x"):
        return False
    a, b = abs(cur), abs(prior)
    if min(a, b) < 1e-9:
        return False
    return max(a, b) / min(a, b) > 50


def is_better(spec, delta):
    if spec.higher_is_better is None or delta is None or delta == 0:
        return None
    return (delta > 0) == spec.higher_is_better


# ---------------------------------------------------------------------------
# Argument resolution (lenient - the LLM passes names, not ids)
# ---------------------------------------------------------------------------

def _spec(metric):
    if metric in M.METRICS:
        return M.METRICS[metric]
    q = " ".join(str(metric).lower().replace("_", " ").split())
    exact = [m for m in M.METRICS.values()
             if q in (m.id.replace("_", " "), m.label.lower()) or q in m.aliases]
    if len(exact) == 1:
        return exact[0]
    hits = M.find_metrics(metric)
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ToolError(f"Metric {metric!r} is ambiguous; use one of: {', '.join(h.id for h in hits[:12])}")
    raise ToolError(f"Unknown metric {metric!r}. Call list_metrics to see valid ids.")


def _entities(entities):
    if not entities:
        return None
    out = []
    for e in entities:
        key = M.entity_key(e) or (e.upper() if e and e.upper() in COMPANIES else None)
        if key is None:
            raise ToolError(f"Unknown company/aggregate {e!r}. Valid: {', '.join(COMPANIES + BENCHMARKS)}")
        out.append(key)
    return out


def _period(store, period):
    if not period:
        p = store.latest_quarter()
        if p is None:
            raise ToolError("No Data Engine has been produced yet.")
        return p
    p = period.strip().upper().replace(" ", "_")
    # Accept 'Q1_FY26-27' as well as 'FY26-27_Q1', and legacy 'FY26' forms.
    parts = p.split("_")
    if len(parts) == 2 and parts[0].startswith("Q"):
        parts = [parts[1], parts[0]]
    try:
        p = S.make_period(parts[0], parts[1] if len(parts) > 1 else None)
        S.parse_period(p)
    except (ValueError, TypeError):
        raise ToolError(f"Unrecognised period {period!r}. Use e.g. 'FY26-27_Q1' or 'FY25-26' (full year).")
    return p


def _row(fact, spec):
    return {"period": fact.period, "entity": fact.entity, "dimension": fact.dimension,
            "value": fact.value, "display": fmt(fact.value, spec.unit), "id": fact.id}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def list_metrics(store, query=None, section=None):
    ids = store.metric_ids()
    specs = M.find_metrics(query) if query else [M.METRICS[i] for i in ids]
    out = []
    for m in specs:
        if m.id not in ids or (section and m.section.lower() != section.lower()):
            continue
        out.append({"id": m.id, "label": m.label, "section": m.section, "unit": m.unit,
                    "higher_is_better": m.higher_is_better, "dimension": m.dimension,
                    "dimensions": store.dimensions(m.id) or None, "description": m.description or None})
    return {"metrics": out}


def list_periods(store):
    return {"quarters": [{"id": p, "label": S.period_label(p)} for p in store.periods("quarter")],
            "annual": [{"id": p, "label": S.period_label(p)} for p in store.periods("annual")],
            "latest_quarter": store.latest_quarter(),
            "note": "Quarterly figures are year-to-date as filed (Q3 = Apr-Dec). Annual = full financial year."}


def list_companies(store):
    present = set(store.entities())
    return {"companies": [{"key": k, "name": M.COMPANY_NAMES[k]} for k in COMPANIES if k in present],
            "aggregates": sorted(present - set(COMPANIES))}


# ---------------------------------------------------------------------------
# Lookups and comparisons
# ---------------------------------------------------------------------------

def get_values(store, metric, entities=None, periods=None, dimension=None):
    spec = _spec(metric)
    ents = _entities(entities)
    pers = [_period(store, p) for p in periods] if periods else [_period(store, None)]
    dim = dimension if dimension is not None else (... if spec.dimension else None)
    facts = store.query(spec.id, entities=ents, periods=pers, dimension=dim)
    return {"metric": spec.id, "label": spec.label, "unit": spec.unit,
            "rows": [_row(f, spec) for f in facts]}


def compare(store, metric, period=None, entities=None, dimension=None):
    """Each company's value this period and the same period last year, its
    change, rank among the companies, and SAHI/Industry benchmarks."""
    spec = _spec(metric)
    period = _period(store, period)
    prior = S.prior_year_period(period)
    ents = _entities(entities) or COMPANIES + BENCHMARKS
    if spec.dimension and dimension is None:
        raise ToolError(f"{spec.id} is split by {spec.dimension}; pass dimension= one of "
                        f"{store.dimensions(spec.id)}, or use breakdown().")
    rows = []
    for e in ents:
        f = store.fact(spec.id, e, period, dimension)
        if f is None:
            continue
        pf = store.fact(spec.id, e, prior, dimension)
        ch = change(f.value, pf.value if pf else None, spec.unit)
        rows.append({"entity": e, "value": f.value, "display": fmt(f.value, spec.unit),
                     "prior": pf.value if pf else None, "prior_display": fmt(pf.value, spec.unit) if pf else None,
                     "change": ch, "improved": is_better(spec, ch["abs"]) if ch else None,
                     "ids": [f.id] + ([pf.id] if pf else [])})
    company_rows = [r for r in rows if r["entity"] in COMPANIES]
    if spec.higher_is_better is not None:
        ordered = sorted(company_rows, key=lambda r: r["value"], reverse=spec.higher_is_better)
        for i, r in enumerate(ordered, 1):
            r["rank"] = i
    vals = [r["value"] for r in company_rows]
    peer = {"median": statistics.median(vals), "mean": statistics.fmean(vals), "n": len(vals)} if vals else None
    if peer:
        peer["median_display"] = fmt(peer["median"], spec.unit)
    return {"metric": spec.id, "label": spec.label, "unit": spec.unit, "dimension": dimension,
            "period": period, "prior_period": prior, "higher_is_better": spec.higher_is_better,
            "rows": rows, "peer_stats": peer}


def breakdown(store, metric, entity, period=None):
    """A split metric (segment/channel/zone mix, portfolio...) for one entity,
    this period vs the same period last year."""
    spec = _spec(metric)
    if not spec.dimension:
        raise ToolError(f"{spec.id} has no breakdown dimension.")
    ent = _entities([entity])[0]
    period = _period(store, period)
    prior = S.prior_year_period(period)
    rows = []
    for f in store.query(spec.id, entities=[ent], periods=[period]):
        pf = store.fact(spec.id, ent, prior, f.dimension)
        ch = change(f.value, pf.value if pf else None, spec.unit)
        rows.append({"dimension": f.dimension, "value": f.value, "display": fmt(f.value, spec.unit),
                     "prior": pf.value if pf else None, "change": ch,
                     "ids": [f.id] + ([pf.id] if pf else [])})
    rows.sort(key=lambda r: -(r["value"] or 0))
    return {"metric": spec.id, "label": spec.label, "unit": spec.unit, "entity": ent,
            "dimension_name": spec.dimension, "period": period, "prior_period": prior, "rows": rows}


def trend(store, metric, entities=None, period_type="annual", dimension=None, quarter=None):
    """Series over time. period_type='annual' uses full years (FY18 onward);
    'quarter' uses quarterly YTD figures - pass quarter='Q1' etc. to keep
    like-for-like points only (Q1s with Q1s)."""
    spec = _spec(metric)
    ents = _entities(entities) or [e for e in COMPANIES + BENCHMARKS if e in store.entities(spec.id)]
    facts = store.query(spec.id, entities=ents, dimension=dimension, period_type=period_type)
    if quarter:
        facts = [f for f in facts if f.period.endswith("_" + quarter.upper())]
    periods = sorted({f.period for f in facts}, key=S.period_sort_key)
    series = []
    for e in ents:
        by_p = {f.period: f for f in facts if f.entity == e}
        if not by_p:
            continue
        pts = [{"period": p, "value": by_p[p].value if p in by_p else None, "id": by_p[p].id if p in by_p else None}
               for p in periods]
        out = {"entity": e, "points": pts}
        known = [pt for pt in pts if pt["value"] is not None]
        if period_type == "annual" and len(known) >= 2 and spec.unit not in ("pct", "x"):
            first, last = known[0], known[-1]
            n = S.parse_period(last["period"])[0] - S.parse_period(first["period"])[0]
            if n > 0 and first["value"] > 0 and last["value"] > 0:
                out["cagr"] = (last["value"] / first["value"]) ** (1 / n) - 1
                out["cagr_span"] = f"{first['period']}..{last['period']}"
        series.append(out)
    return {"metric": spec.id, "label": spec.label, "unit": spec.unit, "period_type": period_type,
            "periods": periods, "series": series}


def top_movers(store, period=None, n=10, entity=None, section=None, include_mix=False):
    """Largest year-on-year moves across every metric for the period, scored
    so ratio moves (pp) and amount growth (%) are comparable: a 1 pp ratio
    move ~ a 10% amount move. Mix/split metrics are excluded unless asked."""
    period = _period(store, period)
    prior = S.prior_year_period(period)
    ents = _entities([entity]) if entity else COMPANIES
    # Materiality: a move is weighted by the line's size relative to the
    # largest peer value of the same metric, and by the company's scale -
    # otherwise a startup insurer's 500% growth off a ₹2 Cr base, or a
    # 266 pp swing in its first-year combined ratio, buries every move that
    # actually matters to the market.
    scale = {c: store.value("gdpi", c, period) for c in COMPANIES}
    top = max((v for v in scale.values() if v), default=None)
    out = []
    for mid in store.metric_ids():
        spec = M.METRICS[mid]
        if (spec.dimension and not include_mix) or spec.unit == "raw":
            continue
        if section and spec.section.lower() != section.lower():
            continue
        cur_facts = store.query(mid, entities=COMPANIES, periods=[period])
        peak = max((abs(f.value) for f in cur_facts), default=0) or 1
        for f in cur_facts:
            if f.entity not in ents:
                continue
            pf = store.fact(mid, f.entity, prior, f.dimension)
            ch = change(f.value, pf.value if pf else None, spec.unit)
            if ch is None:
                continue
            if suspect_jump(f.value, pf.value, spec.unit):
                continue        # see data_quality() - a units/extraction glitch, not a move
            if ch["kind"] == "growth":
                if ch["rel"] is None or abs(pf.value) < 1e-9:
                    continue
                score = min(abs(ch["rel"]), 1.0) * min(abs(f.value) / peak, 1.0) ** 0.5
            else:
                score = min(abs(ch["abs"]) * (10 if ch["kind"] == "pp" else 1), 1.0)
            score *= spec.weight * (0.6 if spec.higher_is_better is None else 1.0)
            if top and scale.get(f.entity):
                score *= (scale[f.entity] / top) ** 0.5
            out.append({"metric": mid, "label": spec.label, "unit": spec.unit, "entity": f.entity,
                        "dimension": f.dimension, "value": f.value, "display": fmt(f.value, spec.unit),
                        "prior": pf.value, "prior_display": fmt(pf.value, spec.unit), "change": ch,
                        "improved": is_better(spec, ch["abs"]), "score": score, "ids": [f.id, pf.id]})
    out.sort(key=lambda r: -r["score"])
    return {"period": period, "prior_period": prior, "movers": out[:max(1, min(int(n), 50))]}


def market_share(store, period=None, base="SAHI"):
    """Each company's share of SAHI (sum of the companies' GDPI) - or of
    Industry GDPI where the market block has it - this period vs last year."""
    period = _period(store, period)
    prior = S.prior_year_period(period)

    def shares(p):
        vals = {c: store.fact("gdpi", c, p) for c in COMPANIES}
        vals = {c: f for c, f in vals.items() if f is not None}
        if base == "SAHI":
            total = sum(f.value for f in vals.values())
            total_ids = [f.id for f in vals.values()]
        else:
            lob = store.query("gdpi_by_lob", entities=["Health Industry"], periods=[p])
            total = sum(f.value for f in lob)
            total_ids = [f.id for f in lob]
        return ({c: (f.value / total if total else None, f.id) for c, f in vals.items()}, total, total_ids)

    cur, cur_total, cur_ids = shares(period)
    pri, pri_total, _ = shares(prior)
    rows = []
    for c, (s, fid) in cur.items():
        ps = pri.get(c, (None, None))[0]
        # Each company's own GDPI and growth ride along, so a caller explaining
        # a share move never has to derive them (an LLM doing that in its head
        # got two of seven growth rates wrong in testing).
        g, gp = store.fact("gdpi", c, period), store.fact("gdpi", c, prior)
        growth = change(g.value, gp.value if gp else None, "cr")
        rows.append({"entity": c, "share": s, "display": fmt(s, "pct"), "prior_share": ps,
                     "change": change(s, ps, "pct"), "gdpi": g.value, "gdpi_display": fmt(g.value, "cr"),
                     "prior_gdpi": gp.value if gp else None, "gdpi_growth": growth["rel"] if growth else None,
                     "gdpi_growth_display": growth["display"] if growth else None,
                     "ids": [fid] + ([gp.id] if gp else []) + cur_ids})
    rows.sort(key=lambda r: -(r["share"] or 0))
    base_growth = change(cur_total, pri_total, "cr")
    return {"period": period, "prior_period": prior, "base": base, "base_total": cur_total,
            "base_total_display": fmt(cur_total, "cr"), "prior_base_total": pri_total,
            "base_growth": base_growth["rel"] if base_growth else None,
            "base_growth_display": base_growth["display"] if base_growth else None, "rows": rows,
            "note": "Shares are computed from the companies' GDPI; SAHI total = sum of the 7 SAHI companies."}


# Registry the agent/API dispatch through: name -> (function, JSON schema of args, description).
_ENT_ARR = {"type": "array", "items": {"type": "string"},
            "description": "Company keys (NBHI, STAR, CARE, CIGNA, ABHI, NARAYANA, GALAXY) or SAHI/Industry."}
_PERIOD = {"type": "string", "description": "e.g. 'FY26-27_Q1' (quarter, YTD) or 'FY25-26' (full year). "
                                           "Omit for the latest quarter."}
TOOLS = {
    "list_metrics": (list_metrics, {"type": "object", "properties": {
        "query": {"type": "string", "description": "Words to search metric names, e.g. 'loss ratio' or 'channel'."},
        "section": {"type": "string"}}},
        "Search the metric catalog. Returns metric ids, units, direction (higher_is_better) and dimensions."),
    "list_periods": (list_periods, {"type": "object", "properties": {}},
                     "Available quarterly and annual periods, and the latest quarter."),
    "list_companies": (list_companies, {"type": "object", "properties": {}},
                       "Company keys/names and aggregate entities available."),
    "get_values": (get_values, {"type": "object", "properties": {
        "metric": {"type": "string"}, "entities": _ENT_ARR,
        "periods": {"type": "array", "items": {"type": "string"}},
        "dimension": {"type": "string", "description": "For split metrics, one segment/channel/etc."}},
        "required": ["metric"]}, "Raw values of one metric for chosen entities and periods."),
    "compare": (compare, {"type": "object", "properties": {
        "metric": {"type": "string"}, "period": _PERIOD, "entities": _ENT_ARR,
        "dimension": {"type": "string"}}, "required": ["metric"]},
        "Compare companies on a metric: value, same-period-last-year value, change, rank, peer median, "
        "SAHI/Industry benchmarks."),
    "breakdown": (breakdown, {"type": "object", "properties": {
        "metric": {"type": "string"}, "entity": {"type": "string"}, "period": _PERIOD},
        "required": ["metric", "entity"]},
        "Split of a mix metric (segment/channel/zone/state/portfolio) for one entity vs last year."),
    "trend": (trend, {"type": "object", "properties": {
        "metric": {"type": "string"}, "entities": _ENT_ARR,
        "period_type": {"type": "string", "enum": ["annual", "quarter"]},
        "dimension": {"type": "string"},
        "quarter": {"type": "string", "description": "With period_type='quarter', keep only this quarter (Q1..Q4)."}},
        "required": ["metric"]}, "Time series of a metric (annual since FY18, or quarterly), with CAGR for amounts."),
    "top_movers": (top_movers, {"type": "object", "properties": {
        "period": _PERIOD, "n": {"type": "integer"}, "entity": {"type": "string"},
        "section": {"type": "string"}, "include_mix": {"type": "boolean"}}},
        "Biggest year-on-year changes across all metrics for a period (optionally one company or section)."),
    "market_share": (market_share, {"type": "object", "properties": {
        "period": _PERIOD, "base": {"type": "string", "enum": ["SAHI", "Health Industry"]}}},
        "Each company's GDPI market share within SAHI (or the health industry) vs last year."),
}


def call(store, name, args):
    if name not in TOOLS:
        raise ToolError(f"Unknown tool {name!r}.")
    fn = TOOLS[name][0]
    try:
        return fn(store, **(args or {}))
    except TypeError as e:
        raise ToolError(f"Bad arguments for {name}: {e}")
