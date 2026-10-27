"""Tests for the v2 analysis layer (analysis/).

Two kinds, per the repo convention:
- synthetic FactStores for the arithmetic (decompositions, ranking, checks),
  so expected values are exact and period-independent;
- live checks against whatever Data Engines are on disk, comparing the store
  to the workbook cells it was built from rather than to hardcoded numbers.
  These skip when no Data Engine has been produced yet.
"""
import glob
import os

import openpyxl
import pytest

from competitor_analysis import paths
from competitor_analysis.analysis import deepdive, insights, metrics as M, store as S, tools as T

P, PY = "FY26-27_Q1", "FY25-26_Q1"


def _f(period, metric, entity, value, dim=None, ptype="quarter"):
    return S.Fact(period, ptype, metric, dim, entity, value, "synthetic.xlsx")


def _store(facts):
    return S.FactStore(facts, [{"file": "synthetic.xlsx", "period": P, "kind": "data_engine"}])


# ---------------------------------------------------------------------------
# Periods and classification
# ---------------------------------------------------------------------------

def test_period_helpers():
    assert S.prior_year_period("FY26-27_Q1") == "FY25-26_Q1"
    assert S.prior_year_period("FY25-26") == "FY24-25"
    assert S.period_label("FY26-27_Q3") == "Q3 FY26-27"
    assert S.period_type("FY25-26") == "annual"
    order = sorted(["FY25-26", "FY26-27_Q1", "FY25-26_Q4", "FY25-26_Q1"], key=S.period_sort_key)
    assert order == ["FY25-26_Q1", "FY25-26_Q4", "FY25-26", "FY26-27_Q1"]
    with pytest.raises(ValueError):
        S.parse_period("2026 Q1")


@pytest.mark.parametrize("row, expected", [
    (("Segment P&L (Health)", "NBHI", "Combined Ratio", None), ("health_combined_ratio", None, "NBHI")),
    (("Segment P&L (Personal Accident)", "Star Health", "UW Profit/(Loss)", None), ("pa_uw_result", None, "STAR")),
    (("Segment wise GDPI - Health", "Segment-wise GDPI mix - SAHI", "Niva Bupa", "Retail"),
     ("segment_mix", "Retail", "NBHI")),
    (("GDPI by Channel", "GDPI by Channel - SAHI", "SAHI", "Brokers"), ("channel_mix", "Brokers", "SAHI")),
    (("Revenue (Segment, Channel, Geographical mix)", "NBHI", "North", None), ("zone_mix", "North", "NBHI")),
    (("Revenue (Segment, Channel, Geographical mix)", "NBHI", "Kerala", None), ("state_mix", "Kerala", "NBHI")),
    (("Revenue (Segment, Channel, Geographical mix)", " Niva bupa health insurance company limited",
      "Revenue Growth (GDPI)", "Health + PA + Travel"), ("gdpi", None, "NBHI")),
    (("Revenue (Segment, Channel, Geographical mix)", "Stand-alone Health sub Total", "Growth %", None),
     ("gdpi_growth", None, "SAHI")),
    (("Debt Portfolio", "ABHI", "Up to 1 year", "Avg. Maturity (5.4 years)"),
     ("debt_maturity_mix", "Up to 1 year", "ABHI")),
    (("Distribution Footprint", "CARE", "Intermediaries", "POS"), ("intermediaries", "POS", "CARE")),
    (("Distribution Footprint", "CARE", "Agents", "Individual Agents"), ("agents", None, "CARE")),
    (("Overall Industry & Market share", "Public GI", "Market share", "Health-Retail"),
     ("gdpi_by_lob", "Health-Retail", "Public GI")),
])
def test_classify(row, expected):
    assert M.classify(*row) == expected


def test_classify_drops_duplicate_and_unknown_entity_rows():
    # Slide 5 repeats each company's GDPI under the market-share block.
    assert M.classify("Overall Industry & Market share", "SAHI Market", "Market share", "Niva Bupa") is None
    assert M.classify("Income Statement", "Some Other Insurer", "PBT", None) is None


def test_unknown_metric_is_registered_not_dropped():
    metric, dim, ent = M.classify("Key Metrics", "NBHI", "Brand New Ratio", None)
    assert ent == "NBHI" and metric in M.METRICS and M.METRICS[metric].unit == "raw"


# ---------------------------------------------------------------------------
# Tools (synthetic)
# ---------------------------------------------------------------------------

def test_compare_ranks_and_changes():
    st = _store([_f(P, "combined_ratio", "NBHI", 1.10), _f(PY, "combined_ratio", "NBHI", 1.15),
                 _f(P, "combined_ratio", "STAR", 1.00), _f(PY, "combined_ratio", "STAR", 0.98),
                 _f(P, "combined_ratio", "CARE", 1.20)])
    out = T.compare(st, "combined_ratio", P)
    rows = {r["entity"]: r for r in out["rows"]}
    assert rows["STAR"]["rank"] == 1 and rows["CARE"]["rank"] == 3      # lower is better
    assert rows["NBHI"]["change"]["kind"] == "pp"
    assert rows["NBHI"]["change"]["abs"] == pytest.approx(-0.05)
    assert rows["NBHI"]["improved"] is True and rows["STAR"]["improved"] is False
    assert rows["CARE"]["change"] is None
    assert out["peer_stats"]["median"] == pytest.approx(1.10)


def test_metric_name_resolution():
    assert T._spec("Combined ratio").id == "combined_ratio"
    assert T._spec("health combined ratio").id == "health_combined_ratio"
    with pytest.raises(T.ToolError):
        T._spec("no such metric at all")


def test_market_share_sums_to_one():
    st = _store([_f(P, "gdpi", "NBHI", 300), _f(P, "gdpi", "STAR", 700),
                 _f(PY, "gdpi", "NBHI", 200), _f(PY, "gdpi", "STAR", 800)])
    out = T.market_share(st, P)
    shares = {r["entity"]: r for r in out["rows"]}
    assert sum(r["share"] for r in out["rows"]) == pytest.approx(1.0)
    assert shares["NBHI"]["change"]["abs"] == pytest.approx(0.10)


def test_top_movers_skips_suspect_jumps():
    st = _store([_f(P, "gdpi", "NBHI", 100), _f(PY, "gdpi", "NBHI", 80),
                 _f(P, "avg_claim_size", "NBHI", 0.5), _f(PY, "avg_claim_size", "NBHI", 900.0)])
    movers = T.top_movers(st, P)["movers"]
    assert [m["metric"] for m in movers] == ["gdpi"]


def test_trend_cagr():
    st = _store([_f("FY22-23", "gwp", "NBHI", 100, ptype="annual"),
                 _f("FY24-25", "gwp", "NBHI", 144, ptype="annual")])
    s = T.trend(st, "gwp", ["NBHI"])["series"][0]
    assert s["cagr"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Insights (synthetic) - every decomposition must add up to what it explains
# ---------------------------------------------------------------------------

def _company_facts():
    facts = []
    for e, (g, gp) in {"NBHI": (130, 100), "STAR": (220, 200)}.items():
        facts += [_f(P, "gdpi", e, g), _f(PY, "gdpi", e, gp)]
    facts += [_f(P, "segment_mix", "NBHI", 0.7, "Retail"), _f(P, "segment_mix", "NBHI", 0.3, "Group"),
              _f(PY, "segment_mix", "NBHI", 0.6, "Retail"), _f(PY, "segment_mix", "NBHI", 0.4, "Group")]
    facts += [_f(P, "combined_ratio", "NBHI", 1.12), _f(PY, "combined_ratio", "NBHI", 1.08),
              _f(P, "loss_ratio", "NBHI", 0.80), _f(PY, "loss_ratio", "NBHI", 0.75),
              _f(P, "expense_ratio", "NBHI", 0.32), _f(PY, "expense_ratio", "NBHI", 0.33)]
    facts += [_f(P, "pbt", "NBHI", -50), _f(PY, "pbt", "NBHI", 10),
              _f(P, "health_uw_result", "NBHI", -120), _f(PY, "health_uw_result", "NBHI", -60),
              _f(P, "investment_income", "NBHI", 90), _f(PY, "investment_income", "NBHI", 80)]
    return facts


def test_growth_drivers_sum_to_growth():
    st = _store(_company_facts())
    ctx = insights._Ctx(st, P)
    ins = insights.growth_insight(ctx, "NBHI", insights._shares(ctx))
    seg = [d["contribution"] for d in ins["drivers"] if d["factor"].endswith("(segment)")]
    assert sum(seg) == pytest.approx(0.30)
    assert ins["sentiment"] == "positive"          # 30% vs SAHI total 16.7% (350/300)


def test_combined_ratio_split_adds_up():
    st = _store(_company_facts())
    ins = insights.profitability_insight(insights._Ctx(st, P), "NBHI")
    assert sum(d["contribution"] for d in ins["drivers"]) == pytest.approx(0.04)
    assert ins["sentiment"] == "negative" and "loss ratio" in ins["headline"]


def test_pbt_bridge_adds_up():
    st = _store(_company_facts())
    ins = insights.earnings_insight(insights._Ctx(st, P), "NBHI")
    assert sum(d["contribution"] for d in ins["drivers"]) == pytest.approx(-60)
    assert ins["implication"] == "Swung from profit to loss this period."


def test_data_checks_flag_duplicated_companies():
    facts = []
    for i, m in enumerate(["gwp", "nwp", "pbt", "claims"]):
        facts += [_f(P, m, "CARE", 100 + i), _f(P, m, "CIGNA", 100 + i), _f(P, m, "NBHI", 50 + i)]
    checks = insights.data_checks(_store(facts), P)
    dup = [c for c in checks if c["kind"] == "duplicate_values"]
    assert len(dup) == 1 and dup[0]["entities"] == ["CARE", "CIGNA"]


# ---------------------------------------------------------------------------
# Live: the store against the workbooks on disk
# ---------------------------------------------------------------------------

_HAS_DE = bool(glob.glob(os.path.join(str(paths.OUTPUT_DIR), "Data_Engine_*_Q[1-4]_*.xlsx")))
live = pytest.mark.skipif(not _HAS_DE, reason="no Data Engine workbooks in artifacts/output")


@pytest.fixture(scope="module")
def store():
    return S.build_store()


@live
def test_store_matches_workbook_cells(store):
    period = store.latest_quarter()
    path = S._latest_data_engines(paths.OUTPUT_DIR)[period]
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    checked = 0
    for r in wb["Data Engine"].iter_rows(min_row=2, values_only=True):
        if not isinstance(r[0], int) or not isinstance(r[7], (int, float)):
            continue
        cls = M.classify(r[1], r[2], r[3], r[4])
        if cls is None or M.METRICS[cls[0]].unit == "raw":
            continue
        metric, dim, ent = cls
        f = store.fact(metric, ent, period, dim)
        assert f is not None, (metric, dim, ent)
        if f.source == os.path.basename(path) and M.METRICS[metric].scale == 1:
            # The first row for an id wins in-file; duplicated rows (e.g. PBT
            # on two slides) must agree for this to hold.
            checked += f.value == pytest.approx(r[7])
    wb.close()
    assert checked > 200


@live
def test_own_workbook_beats_later_prior_column(store):
    """Where a period's own workbook has a company's figure, that copy wins
    over a later workbook's PRIOR column. (A company missing from an older
    template - e.g. Galaxy before it was added - legitimately falls back.)"""
    files = S._latest_data_engines(paths.OUTPUT_DIR)
    for period, path in files.items():
        own_entities = {M.classify(r[1], r[2], r[3], r[4])[2] for r in S._read_data_engine(path)
                        if isinstance(r[7], (int, float)) and M.classify(r[1], r[2], r[3], r[4])
                        and M.classify(r[1], r[2], r[3], r[4])[0] == "gwp"}
        for f in store.query("gwp", periods=[period]):
            if f.entity in own_entities:
                assert f.source == os.path.basename(path), (period, f.entity, f.source)


@live
def test_history_wins_for_full_years(store):
    annual = store.query("gdpi", period_type="annual")
    if not os.path.isfile(str(paths.HISTORICAL_TRENDS_WORKBOOK)) or not annual:
        pytest.skip("no historical workbook")
    hist = os.path.basename(str(paths.HISTORICAL_TRENDS_WORKBOOK))
    assert any(f.source == hist for f in annual)


@live
def test_highlights_and_deepdive_build(store):
    report = insights.build(store)
    assert report["insights"] and all(i["headline"] for i in report["insights"])
    for i in report["insights"]:
        for ev in i["evidence"]:
            assert all(store.get(x) for x in ev["ids"])
    dd = deepdive.build(store, "NBHI")
    assert dd["kpis"] and dd["sections"]
