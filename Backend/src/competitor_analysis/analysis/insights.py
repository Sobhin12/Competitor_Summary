"""
Quarter Highlights: rule-based insights - not just what moved, but why.

Each generator turns a group of related facts into one structured insight:
the observation, the drivers that explain it (decompositions that add up
to the observed change), its context (rank, peers, history), and a
deterministic implication. The optional LLM narrative (narrative.py) writes
prose over these objects; it never adds numbers of its own.

Decompositions used:
  growth      GDPI growth = sum over segments/channels of
              (mix_cur * GDPI_cur - mix_prior * GDPI_prior) / GDPI_prior
  combined    dCR = dLoss ratio + dExpense ratio
  earnings    dPBT = dUW result (Health, PA, Travel) + dInvestment income
              + other (residual: shareholders' account items, tax-free etc.)
  share       d(share of SAHI) - from growth relative to the SAHI total
"""
import statistics

from competitor_analysis.analysis import metrics as M
from competitor_analysis.analysis import store as S
from competitor_analysis.analysis import tools as T

COMPANIES = M.COMPANY_KEYS
# Companies still in their first years of writing business: tiny bases make
# their ratios swing by hundreds of points (see KNOWN_ISSUES.md §2).
EARLY_STAGE = {"NARAYANA", "GALAXY"}
SOLVENCY_MIN = 1.5


def _name(e):
    return M.COMPANY_NAMES.get(e, e)


def _pp(x):
    return f"{x * 100:+.1f} pp"


def _pct(x):
    return f"{x * 100:.1f}%"


class _Ctx:
    """Shared per-period lookups so each generator stays short."""

    def __init__(self, store, period):
        self.s = store
        self.p = period
        self.py = S.prior_year_period(period)
        self.gdpi = {c: store.value("gdpi", c, period) for c in COMPANIES}
        top = max((v for v in self.gdpi.values() if v), default=None)
        self.materiality = {c: ((v / top) ** 0.5 if v and top else 0.3) for c, v in self.gdpi.items()}

    def fact(self, metric, e, prior=False, dim=None):
        return self.s.fact(metric, e, self.py if prior else self.p, dim)

    def val(self, metric, e, prior=False, dim=None):
        f = self.fact(metric, e, prior, dim)
        return f.value if f else None

    def evidence(self, metric, e, dim=None, label=None):
        spec = M.METRICS[metric]
        f, pf = self.fact(metric, e, False, dim), self.fact(metric, e, True, dim)
        if f is None:
            return None
        ch = T.change(f.value, pf.value if pf else None, spec.unit)
        return {"metric": metric, "label": label or (spec.label + (f" - {dim}" if dim else "")), "entity": e,
                "unit": spec.unit, "value": f.value, "display": T.fmt(f.value, spec.unit),
                "prior": pf.value if pf else None, "prior_display": T.fmt(pf.value, spec.unit) if pf else None,
                "change_display": ch["display"] if ch else None,
                "ids": [f.id] + ([pf.id] if pf else [])}

    def rank(self, metric, e):
        spec = M.METRICS[metric]
        vals = {c: self.val(metric, c) for c in COMPANIES}
        vals = {c: v for c, v in vals.items() if v is not None}
        if e not in vals or spec.higher_is_better is None:
            return None, len(vals)
        order = sorted(vals, key=vals.get, reverse=spec.higher_is_better)
        return order.index(e) + 1, len(vals)

    def peer_median(self, metric, exclude_early=True):
        vals = [self.val(metric, c) for c in COMPANIES if not (exclude_early and c in EARLY_STAGE)]
        vals = [v for v in vals if v is not None]
        return statistics.median(vals) if vals else None

    def caveats(self, e):
        out = []
        if e in EARLY_STAGE:
            out.append(f"{_name(e)} is an early-stage insurer - small base, so ratios and growth rates swing sharply.")
        return out


def _insight(theme, entity, headline, score, sentiment, *, evidence=(), drivers=(), context=(), implication=None,
             caveats=(), chart=None, key=None):
    return {"id": key or f"{theme}:{entity}", "theme": theme, "entity": entity,
            "entity_name": _name(entity), "headline": headline, "score": round(score, 4),
            "sentiment": sentiment, "evidence": [e for e in evidence if e], "drivers": list(drivers),
            "context": [c for c in context if c], "implication": implication, "caveats": list(caveats),
            "chart": chart}


def _mix_contributions(ctx, e, mix_metric):
    """[(dimension, contribution to GDPI growth as a fraction of prior GDPI, ids)]."""
    g, gp = ctx.fact("gdpi", e), ctx.fact("gdpi", e, prior=True)
    if not g or not gp or not gp.value:
        return []
    out = []
    for f in ctx.s.query(mix_metric, entities=[e], periods=[ctx.p]):
        pf = ctx.fact(mix_metric, e, True, f.dimension)
        if pf is None:
            continue
        contrib = (f.value * g.value - pf.value * gp.value) / gp.value
        out.append((f.dimension, contrib, [f.id, pf.id, g.id, gp.id]))
    total_mix = sum(f.value for f in ctx.s.query(mix_metric, entities=[e], periods=[ctx.p]))
    # A mix that doesn't sum to ~100% (missing channel rows) can't explain growth.
    if not out or not 0.95 <= total_mix <= 1.05:
        return []
    return sorted(out, key=lambda t: -abs(t[1]))


# ---------------------------------------------------------------------------
# Company-level generators
# ---------------------------------------------------------------------------

def growth_insight(ctx, e, shares):
    g, gp = ctx.val("gdpi", e), ctx.val("gdpi", e, prior=True)
    if g is None or not gp:
        return None
    growth = g / gp - 1
    sahi_growth = shares["sahi_growth"]
    share = next((r for r in shares["rows"] if r["entity"] == e), None)
    d_share = share["change"]["abs"] if share and share["change"] else None
    gap = growth - sahi_growth if sahi_growth is not None else None

    seg = _mix_contributions(ctx, e, "segment_mix")
    chn = _mix_contributions(ctx, e, "channel_mix")
    drivers = [{"factor": f"{d} (segment)", "contribution": c, "display": _pp(c), "ids": ids}
               for d, c, ids in seg if abs(c) >= 0.005]
    drivers += [{"factor": f"{d} (channel)", "contribution": c, "display": _pp(c), "ids": ids}
                for d, c, ids in chn[:3] if abs(c) >= 0.005]

    verb = "gained" if (d_share or 0) > 0 else "lost"
    head = f"{_name(e)} grew GDPI {_pct(growth)}"
    if gap is not None:
        head += f", {abs(gap) * 100:.1f} pp {'ahead of' if gap >= 0 else 'behind'} SAHI ({_pct(sahi_growth)})"
    if d_share is not None and abs(d_share) >= 0.0005:
        head += f" - {verb} {abs(d_share) * 100:.1f} pp of SAHI share"

    top_seg = seg[0] if seg else None
    if gap is None:
        implication = None
    elif gap >= 0.02:
        implication = (f"Outgrowing the SAHI market and taking share"
                       + (f"; {top_seg[0]} is the main engine ({_pp(top_seg[1])} of the {_pct(growth)} growth)."
                          if top_seg else "."))
    elif gap <= -0.02:
        weakest = min(seg, key=lambda t: t[1]) if seg else None
        implication = ("Growing slower than the SAHI market, so losing relative position"
                       + (f"; weakest contributor is {weakest[0]} ({_pp(weakest[1])})." if weakest else "."))
    else:
        implication = "Growing broadly in line with the SAHI market - share roughly stable."

    context = []
    hist = ctx.s.query("gdpi_growth", entities=[e], period_type="quarter")
    same_q = [f for f in hist if f.period.split("_")[-1] == ctx.p.split("_")[-1] and f.period != ctx.p]
    if same_q:
        context.append("Same quarter in earlier years: " + ", ".join(
            f"{S.period_label(f.period)} {_pct(f.value)}" for f in sorted(same_q, key=lambda f: S.period_sort_key(f.period))))
    r, n = ctx.rank("gdpi", e)
    if r:
        context.append(f"#{r} of {n} by GDPI size ({T.fmt(g, 'cr')}).")

    chart = None
    if seg:
        chart = {"type": "waterfall", "title": f"{_name(e)}: what drove {_pct(growth)} GDPI growth", "unit": "pp",
                 "categories": [d for d, _, _ in seg], "series": [{"name": "Contribution",
                                                                   "values": [round(c * 100, 2) for _, c, _ in seg]}]}
    score = (min(abs(gap or 0), 0.3) * 3 + abs(d_share or 0) * 40) * ctx.materiality[e]
    if e in EARLY_STAGE:
        score *= 0.3
    sentiment = "positive" if (gap or 0) > 0.02 else "negative" if (gap or 0) < -0.02 else "neutral"
    return _insight("Growth & market share", e, head, score, sentiment,
                    evidence=[ctx.evidence("gdpi", e), ctx.evidence("gdpi_growth", "SAHI")],
                    drivers=drivers, context=context, implication=implication,
                    caveats=ctx.caveats(e), chart=chart)


def profitability_insight(ctx, e):
    cr, crp = ctx.val("combined_ratio", e), ctx.val("combined_ratio", e, prior=True)
    lr, lrp = ctx.val("loss_ratio", e), ctx.val("loss_ratio", e, prior=True)
    er, erp = ctx.val("expense_ratio", e), ctx.val("expense_ratio", e, prior=True)
    if cr is None or crp is None:
        return None
    d = cr - crp
    drivers = []
    if None not in (lr, lrp, er, erp):
        drivers = [{"factor": "Loss ratio", "contribution": lr - lrp, "display": _pp(lr - lrp)},
                   {"factor": "Expense ratio", "contribution": er - erp, "display": _pp(er - erp)}]
    better = d < 0
    head = (f"{_name(e)}'s combined ratio {'improved' if better else 'worsened'} {abs(d) * 100:.1f} pp "
            f"to {_pct(cr)}")
    if drivers:
        main = max(drivers, key=lambda x: abs(x["contribution"]))
        head += f", mainly on {main['factor'].lower()} ({main['display']})"

    r, n = ctx.rank("combined_ratio", e)
    med = ctx.peer_median("combined_ratio")
    context = []
    if r:
        context.append(f"#{r} of {n} (lowest combined ratio = #1).")
    if med is not None:
        context.append(f"Peer median {_pct(med)} (excluding early-stage insurers).")
    hist = ctx.s.query("combined_ratio", entities=[e], period_type="annual")
    if len(hist) >= 3:
        last3 = sorted(hist, key=lambda f: S.period_sort_key(f.period))[-3:]
        context.append("Full-year trend: " + ", ".join(f"{f.period} {_pct(f.value)}" for f in last3))

    if cr < 1 <= crp:
        implication = "Crossed below 100% - now making an underwriting profit."
    elif cr >= 1 > crp:
        implication = "Crossed above 100% - underwriting has turned loss-making."
    elif cr < 1:
        implication = "Underwriting profitably; profit before investment income."
    else:
        implication = (f"Still loss-making at the underwriting level - every ₹100 of earned premium costs "
                       f"₹{cr * 100:.0f} in claims and expenses, so profit relies on investment income.")
    if len(drivers) == 2 and drivers[0]["contribution"] * drivers[1]["contribution"] < 0:
        up = next(x for x in drivers if x["contribution"] > 0)
        dn = next(x for x in drivers if x["contribution"] < 0)
        implication += f" {dn['factor']} gains are offsetting pressure on {up['factor'].lower()}."

    score = min(abs(d), 0.15) * 6 * ctx.materiality[e]
    chart = {"type": "bar", "title": "Change in combined ratio, split", "unit": "pp",
             "categories": [x["factor"] for x in drivers] + ["Combined ratio"],
             "series": [{"name": "Change", "values": [round(x["contribution"] * 100, 2) for x in drivers]
                         + [round(d * 100, 2)]}]} if drivers else None
    return _insight("Profitability", e, head, score, "positive" if better else "negative",
                    evidence=[ctx.evidence("combined_ratio", e), ctx.evidence("loss_ratio", e),
                              ctx.evidence("expense_ratio", e)],
                    drivers=drivers, context=context, implication=implication, caveats=ctx.caveats(e), chart=chart)


def earnings_insight(ctx, e):
    pbt, pbtp = ctx.fact("pbt", e), ctx.fact("pbt", e, prior=True)
    if not pbt or not pbtp:
        return None
    d = pbt.value - pbtp.value
    parts = [("Health underwriting", "health_uw_result"), ("PA underwriting", "pa_uw_result"),
             ("Travel underwriting", "travel_uw_result"), ("Investment income", "investment_income")]
    drivers, explained = [], 0.0
    for label, m in parts:
        a, b = ctx.val(m, e), ctx.val(m, e, prior=True)
        if a is None or b is None:
            continue
        drivers.append({"factor": label, "contribution": a - b, "display": f"{a - b:+,.0f} Cr"})
        explained += a - b
    if not drivers:
        return None
    drivers.append({"factor": "Other (shareholders' a/c, etc.)", "contribution": d - explained,
                    "display": f"{d - explained:+,.0f} Cr"})
    main = max(drivers, key=lambda x: abs(x["contribution"]))
    head = (f"{_name(e)} PBT {'rose' if d >= 0 else 'fell'} {abs(d):,.0f} Cr to {T.fmt(pbt.value, 'cr')}"
            f" - biggest driver: {main['factor'].lower()} ({main['display']})")
    if pbt.value < 0 <= pbtp.value:
        implication = "Swung from profit to loss this period."
    elif pbt.value >= 0 > pbtp.value:
        implication = "Turned profitable this period."
    elif main["factor"] == "Investment income" and d > 0:
        implication = "Earnings growth leans on investment income rather than core underwriting."
    elif "underwriting" in main["factor"] and main["contribution"] > 0:
        implication = "Earnings improvement is coming from the core insurance business - higher-quality profit."
    else:
        implication = None
    gwp = ctx.val("gwp", e) or 0
    score = min(abs(d) / max(gwp, 1), 0.2) * 5 * ctx.materiality[e]
    chart = {"type": "waterfall", "title": f"{_name(e)} PBT bridge ({S.period_label(ctx.py)} → {S.period_label(ctx.p)})",
             "unit": "cr", "categories": [x["factor"] for x in drivers],
             "series": [{"name": "Change", "values": [round(x["contribution"], 2) for x in drivers]}],
             "start": {"label": S.period_label(ctx.py), "value": round(pbtp.value, 2)},
             "end": {"label": S.period_label(ctx.p), "value": round(pbt.value, 2)}}
    return _insight("Earnings", e, head, score, "positive" if d >= 0 else "negative",
                    evidence=[ctx.evidence("pbt", e), ctx.evidence("investment_income", e),
                              ctx.evidence("health_uw_result", e)],
                    drivers=drivers, implication=implication, caveats=ctx.caveats(e), chart=chart)


def mix_shift_insight(ctx, e, metric, theme, min_pp=0.02):
    rows = []
    for f in ctx.s.query(metric, entities=[e], periods=[ctx.p]):
        pf = ctx.fact(metric, e, True, f.dimension)
        if pf is not None:
            rows.append((f.dimension, f.value, pf.value, f.id, pf.id))
    # Both periods' shares must look like a real mix (each 0-100%, summing
    # to ~100%) - otherwise it's an extraction glitch, not a shift.
    if not rows or not all(0 <= r[1] <= 1.01 and 0 <= r[2] <= 1.01 for r in rows):
        return None
    if not (0.95 <= sum(r[1] for r in rows) <= 1.05 and 0.95 <= sum(r[2] for r in rows) <= 1.05):
        return None
    dim, cur, pri, fid, pid = max(rows, key=lambda r: abs(r[1] - r[2]))
    d = cur - pri
    if abs(d) < min_pp:
        return None
    word = M.METRICS[metric].dimension.lower()
    head = (f"{_name(e)} shifted {abs(d) * 100:.1f} pp of its business {'toward' if d > 0 else 'away from'} "
            f"{dim} ({_pct(pri)} → {_pct(cur)} of GDPI)")
    context = []
    sahi = ctx.val(metric, "SAHI", dim=dim)
    if sahi is not None:
        context.append(f"SAHI average {dim} share: {_pct(sahi)}.")
    peers = [(c, ctx.val(metric, c, dim=dim)) for c in COMPANIES if c != e]
    peers = [(c, v) for c, v in peers if v is not None]
    if peers:
        hi = max(peers, key=lambda t: t[1])
        context.append(f"Highest {dim} share among peers: {_name(hi[0])} {_pct(hi[1])}.")
    implication = None
    if metric == "channel_mix":
        cp = ctx.val("channel_commission_pct", e, dim=dim)
        if cp is not None:
            implication = f"{dim} costs {_pct(cp)} of its premium in commission - watch the effect on the expense ratio."
    elif metric == "segment_mix" and dim in ("Retail", "Group"):
        implication = (f"Portfolio tilting {'toward' if (dim == 'Retail') == (d > 0) else 'away from'} retail - "
                       f"retail usually carries higher acquisition cost but stickier, better-priced business.")
    others = sorted(rows, key=lambda r: -abs(r[1] - r[2]))[1:3]
    drivers = [{"factor": dim, "contribution": d, "display": _pp(d), "ids": [fid, pid]}] + [
        {"factor": r[0], "contribution": r[1] - r[2], "display": _pp(r[1] - r[2]), "ids": [r[3], r[4]]}
        for r in others if abs(r[1] - r[2]) >= 0.005]
    chart = {"type": "grouped_bar", "title": f"{_name(e)} {word} mix", "unit": "pct",
             "categories": [r[0] for r in rows],
             "series": [{"name": S.period_label(ctx.py), "values": [r[2] for r in rows]},
                        {"name": S.period_label(ctx.p), "values": [r[1] for r in rows]}]}
    score = min(abs(d), 0.15) * 3 * ctx.materiality[e]
    return _insight(theme, e, head, score, "neutral", evidence=[ctx.evidence(metric, e, dim)], drivers=drivers,
                    context=context, implication=implication, caveats=ctx.caveats(e), chart=chart,
                    key=f"{theme}:{metric}:{e}")


def capital_insight(ctx, e):
    s, sp = ctx.val("solvency_ratio", e), ctx.val("solvency_ratio", e, prior=True)
    if s is None:
        return None
    d = s - sp if sp is not None else None
    if s >= 1.8 and (d is None or d > -0.3):
        return None
    head = f"{_name(e)} solvency at {s:.2f}x" + (f" ({d:+.2f}x YoY)" if d is not None else "")
    pbt = ctx.val("pbt", e)
    context = [f"Regulatory minimum {SOLVENCY_MIN:.1f}x; headroom {s - SOLVENCY_MIN:.2f}x."]
    if pbt is not None and pbt < 0:
        context.append(f"Loss-making this period (PBT {T.fmt(pbt, 'cr')}) - losses erode capital.")
    implication = ("Headroom is thin - sustaining this growth will likely need fresh capital." if s < 1.8 else
                   "Headroom narrowing as growth consumes capital; watch for a capital raise.")
    score = (0.4 if s < 1.8 else 0.2) * ctx.materiality[e]
    return _insight("Capital", e, head, score, "negative",
                    evidence=[ctx.evidence("solvency_ratio", e), ctx.evidence("pbt", e)],
                    context=context, implication=implication, caveats=ctx.caveats(e))


# ---------------------------------------------------------------------------
# Market-level themes
# ---------------------------------------------------------------------------

def _shares(ctx):
    ms = T.market_share(ctx.s, ctx.p)
    sahi_g = ctx.val("gdpi_growth", "SAHI")
    if sahi_g is None and ms["prior_base_total"]:
        sahi_g = ms["base_total"] / ms["prior_base_total"] - 1
    ms["sahi_growth"] = sahi_g
    return ms


def market_insights(ctx, shares):
    out = []
    sahi_g, ind_g = shares["sahi_growth"], ctx.val("gdpi_growth", "Industry")
    if sahi_g is not None and ind_g is not None:
        gap = sahi_g - ind_g
        out.append(_insight(
            "Market", "SAHI",
            f"Standalone health insurers grew {_pct(sahi_g)} vs {_pct(ind_g)} for the whole industry",
            0.6 + abs(gap), "neutral",
            evidence=[ctx.evidence("gdpi_growth", "SAHI"), ctx.evidence("gdpi_growth", "Industry")],
            implication=("SAHIs are taking share from multi-line general insurers in health." if gap > 0 else
                         "Multi-line general insurers are outgrowing the SAHIs in health."),
            key="Market:sahi-vs-industry"))

    rows = [r for r in shares["rows"] if r["change"]]
    if rows:
        gainer = max(rows, key=lambda r: r["change"]["abs"])
        loser = min(rows, key=lambda r: r["change"]["abs"])
        out.append(_insight(
            "Market", "SAHI",
            f"Share shift within SAHI: {_name(gainer['entity'])} gained most ({gainer['change']['display']}), "
            f"{_name(loser['entity'])} gave up most ({loser['change']['display']})",
            0.5, "neutral",
            context=[f"{_name(r['entity'])} {r['display']} ({r['change']['display']})" for r in shares["rows"]],
            implication=None,
            chart={"type": "bar", "title": "Change in SAHI market share", "unit": "pp",
                   "categories": [r["entity"] for r in rows],
                   "series": [{"name": "Δ share", "values": [round(r["change"]["abs"] * 100, 2) for r in rows]}]},
            key="Market:share-shift"))

    crs = []
    for c in COMPANIES:
        a, b = ctx.val("combined_ratio", c), ctx.val("combined_ratio", c, prior=True)
        if a is not None and b is not None and c not in EARLY_STAGE:
            crs.append((c, a, b))
    if crs:
        improved = [c for c, a, b in crs if a < b]
        under_100 = [c for c, a, _ in crs if a < 1]
        out.append(_insight(
            "Market", "SAHI",
            f"{len(improved)} of {len(crs)} established insurers improved their combined ratio; "
            f"{len(under_100)} {'is' if len(under_100) == 1 else 'are'} below 100%",
            0.45, "positive" if len(improved) * 2 > len(crs) else "negative",
            context=[f"{_name(c)} {_pct(a)} ({_pp(a - b)})" for c, a, b in sorted(crs, key=lambda t: t[1])],
            implication=("Underwriting discipline is improving across the sector." if len(improved) * 2 > len(crs)
                         else "Claims/cost pressure is broad-based across the sector, not company-specific."),
            key="Market:cr-breadth"))

    rs, rsp = ctx.val("segment_mix", "SAHI", dim="Retail"), ctx.val("segment_mix", "SAHI", True, "Retail")
    if rs is not None and rsp is not None and abs(rs - rsp) >= 0.005:
        out.append(_insight(
            "Market", "SAHI", f"Retail is {_pct(rs)} of SAHI GDPI ({_pp(rs - rsp)} YoY)", 0.3, "neutral",
            evidence=[ctx.evidence("segment_mix", "SAHI", "Retail")],
            implication=("The SAHI market is becoming more retail-led." if rs > rsp
                         else "Group business is gaining weight in the SAHI mix."),
            key="Market:retail-share"))
    return out


# ---------------------------------------------------------------------------
# Data checks - surfaced alongside insights so nobody headlines a glitch
# ---------------------------------------------------------------------------

def data_checks(store, period):
    checks = []
    facts = store.query(periods=[period], entities=COMPANIES)
    by_ent = {}
    for f in facts:
        by_ent.setdefault(f.entity, {})[(f.metric, f.dimension)] = f.value
    for i, a in enumerate(COMPANIES):
        for b in COMPANIES[i + 1:]:
            va, vb = by_ent.get(a, {}), by_ent.get(b, {})
            same = [k for k in va.keys() & vb.keys()
                    if va[k] == vb[k] and va[k] not in (0, None) and M.METRICS[k[0]].unit != "count"]
            if len(same) >= 3:
                labels = sorted({M.METRICS[m].label for m, _ in same})
                checks.append({"kind": "duplicate_values", "entities": [a, b],
                               "message": f"{_name(a)} and {_name(b)} have identical values on {len(same)} "
                                          f"data points ({', '.join(labels[:5])}{'...' if len(labels) > 5 else ''}) "
                                          f"- likely an extraction copy error; treat those comparisons with caution.",
                               "metrics": sorted({m for m, _ in same})})
    prior = S.prior_year_period(period)
    for f in facts:
        pf = store.fact(f.metric, f.entity, prior, f.dimension)
        spec = M.METRICS[f.metric]
        if pf and T.suspect_jump(f.value, pf.value, spec.unit):
            checks.append({"kind": "suspect_jump", "entities": [f.entity],
                           "message": f"{_name(f.entity)} {spec.label}: {T.fmt(pf.value, spec.unit)} → "
                                      f"{T.fmt(f.value, spec.unit)} - a >50x change, likely a units/extraction issue.",
                           "metrics": [f.metric]})
    return checks


# ---------------------------------------------------------------------------

def build(store, period=None, max_per_theme=4):
    period = T._period(store, period)
    ctx = _Ctx(store, period)
    shares = _shares(ctx)
    checks = data_checks(store, period)
    suspect = {(c["entities"][0], m) for c in checks if c["kind"] == "suspect_jump" for m in c["metrics"]}
    dup_pairs = [c for c in checks if c["kind"] == "duplicate_values"]

    insights = market_insights(ctx, shares)
    for e in COMPANIES:
        if ctx.gdpi.get(e) is None:
            continue
        for ins in (growth_insight(ctx, e, shares), profitability_insight(ctx, e), earnings_insight(ctx, e),
                    mix_shift_insight(ctx, e, "segment_mix", "Portfolio mix"),
                    mix_shift_insight(ctx, e, "channel_mix", "Distribution"), capital_insight(ctx, e)):
            if ins is None:
                continue
            if any((e, ev["metric"]) in suspect for ev in ins["evidence"]):
                continue
            for c in dup_pairs:
                if e in c["entities"] and any(ev["metric"] in c["metrics"] for ev in ins["evidence"]):
                    other = next(x for x in c["entities"] if x != e)
                    ins["caveats"].append(f"Some of these figures are identical to {_name(other)}'s - "
                                          f"possible extraction error.")
                    ins["score"] *= 0.5
            insights.append(ins)

    insights.sort(key=lambda i: -i["score"])
    themes = {}
    for ins in insights:
        themes.setdefault(ins["theme"], [])
        if len(themes[ins["theme"]]) < max_per_theme or ins["theme"] == "Market":
            themes[ins["theme"]].append(ins)
    kept = [i for lst in themes.values() for i in lst]
    kept.sort(key=lambda i: -i["score"])
    return {"period": period, "period_label": S.period_label(period), "prior_period": ctx.py,
            "prior_period_label": S.period_label(ctx.py), "insights": kept,
            "all_insights_count": len(insights), "data_checks": checks,
            "market_share": shares}
