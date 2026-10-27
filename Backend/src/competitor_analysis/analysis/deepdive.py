"""
Competitor deep-dive: everything known about one company for a period -
headline KPIs, every metric grouped by section with its rank among peers,
the peer median and the SAHI figure, mix breakdowns, full-year trends, where
it leads/lags, and the insights that concern it.
"""
import statistics

from competitor_analysis.analysis import insights as I
from competitor_analysis.analysis import metrics as M
from competitor_analysis.analysis import store as S
from competitor_analysis.analysis import tools as T

KPIS = ["gdpi", "gdpi_growth", "combined_ratio", "pbt", "solvency_ratio", "claim_settlement_ratio"]
TREND_METRICS = ["gdpi", "gwp", "pbt", "combined_ratio", "loss_ratio", "expense_ratio", "solvency_ratio", "roe"]
BREAKDOWNS = ["segment_mix", "channel_mix", "zone_mix"]


def _metric_row(store, metric, e, period, prior):
    spec = M.METRICS[metric]
    f = store.fact(metric, e, period)
    if f is None:
        return None
    pf = store.fact(metric, e, prior)
    ch = T.change(f.value, pf.value if pf else None, spec.unit)
    peers = {c: store.value(metric, c, period) for c in M.COMPANY_KEYS}
    peers = {c: v for c, v in peers.items() if v is not None}
    rank = None
    if spec.higher_is_better is not None and len(peers) > 1:
        order = sorted(peers, key=peers.get, reverse=spec.higher_is_better)
        rank = order.index(e) + 1
    med = statistics.median(peers.values()) if peers else None
    sahi = store.value(metric, "SAHI", period)
    return {"metric": metric, "label": spec.label, "unit": spec.unit, "higher_is_better": spec.higher_is_better,
            "value": f.value, "display": T.fmt(f.value, spec.unit),
            "prior": pf.value if pf else None, "prior_display": T.fmt(pf.value, spec.unit) if pf else None,
            "change": ch, "improved": T.is_better(spec, ch["abs"]) if ch else None,
            "rank": rank, "of": len(peers), "peer_median": med, "peer_median_display": T.fmt(med, spec.unit),
            "sahi": sahi, "sahi_display": T.fmt(sahi, spec.unit) if sahi is not None else None,
            "ids": [f.id] + ([pf.id] if pf else [])}


def build(store, company, period=None):
    e = T._entities([company])[0]
    if e not in M.COMPANY_KEYS:
        raise T.ToolError(f"{company!r} is not one of the 7 SAHI companies.")
    period = T._period(store, period)
    prior = S.prior_year_period(period)

    sections = {}
    for metric in store.metric_ids():
        spec = M.METRICS[metric]
        if spec.dimension or spec.unit == "raw":
            continue
        row = _metric_row(store, metric, e, period, prior)
        if row:
            sections.setdefault(spec.section, []).append(row)
    ordered_sections = [{"section": s, "rows": sections[s]} for s in M.SECTIONS if s in sections]

    kpis = [r for r in (_metric_row(store, m, e, period, prior) for m in KPIS) if r]
    share = next((r for r in T.market_share(store, period)["rows"] if r["entity"] == e), None)
    if share:
        kpis.insert(1, {"metric": "sahi_share", "label": "SAHI market share", "unit": "pct",
                        "value": share["share"], "display": share["display"], "change": share["change"],
                        "improved": (share["change"]["abs"] > 0) if share["change"] else None, "ids": share["ids"]})

    # Where it leads / lags: top-2 and bottom-2 ranks on directional metrics,
    # ignoring 2-company comparisons and the best-effort estimates. Absolute
    # amounts/counts are excluded (the biggest insurer always "ranks last" on
    # claims and overheads) except profit lines, where size is the point.
    def _comparable(r):
        unit = r["unit"]
        return unit not in ("cr", "count") or r["metric"] in ("pbt", "pat") or r["metric"].endswith("_uw_result")
    ranked = [r for s in ordered_sections for r in s["rows"]
              if r["rank"] and r["of"] >= 4 and M.METRICS[r["metric"]].weight >= 0.8 and _comparable(r)]
    strengths = sorted([r for r in ranked if r["rank"] <= 2], key=lambda r: r["rank"])
    weaknesses = sorted([r for r in ranked if r["rank"] >= r["of"] - 1], key=lambda r: -r["rank"])

    breakdowns = []
    for m in BREAKDOWNS:
        try:
            b = T.breakdown(store, m, e, period)
        except T.ToolError:
            continue
        if b["rows"]:
            sahi = {f.dimension: f.value for f in store.query(m, entities=["SAHI"], periods=[period])}
            for r in b["rows"]:
                r["sahi"] = sahi.get(r["dimension"])
            breakdowns.append(b)

    trends = []
    for m in TREND_METRICS:
        t = T.trend(store, m, [e], period_type="annual")
        if t["series"] and sum(p["value"] is not None for p in t["series"][0]["points"]) >= 2:
            peer = T.trend(store, m, None, period_type="annual")
            med = []
            for i, p in enumerate(t["periods"]):
                vals = [s["points"][peer["periods"].index(p)]["value"] for s in peer["series"]
                        if s["entity"] in M.COMPANY_KEYS and p in peer["periods"]]
                vals = [v for v in vals if v is not None]
                med.append(statistics.median(vals) if vals else None)
            t["peer_median"] = med
            trends.append(t)

    report = I.build(store, period, max_per_theme=99)
    company_insights = [i for i in report["insights"] if i["entity"] == e]
    checks = [c for c in report["data_checks"] if e in c["entities"]]

    return {"company": e, "name": M.COMPANY_NAMES[e], "period": period, "period_label": S.period_label(period),
            "prior_period": prior, "prior_period_label": S.period_label(prior),
            "early_stage": e in I.EARLY_STAGE, "kpis": kpis, "sections": ordered_sections,
            "strengths": strengths[:6], "weaknesses": weaknesses[:6], "breakdowns": breakdowns,
            "trends": trends, "insights": company_insights, "data_checks": checks}
