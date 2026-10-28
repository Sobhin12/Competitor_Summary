"""Tests for the formula engine (analysis/formula.py).

Synthetic FactStores give exact, period-independent expected values.
"""
import pytest

from competitor_analysis.analysis import formula as F, store as S

P, PY = "FY26-27_Q2", "FY25-26_Q2"
PQ, PQY = "FY26-27_Q1", "FY25-26_Q1"
PEERS = ["NBHI", "STAR", "CARE", "CIGNA", "ABHI"]


def _f(period, metric, entity, value):
    return S.Fact(period, "quarter", metric, None, entity, value, "synthetic.xlsx")


def _store(per_company, periods):
    """per_company: {entity: {metric: {period: value}}}"""
    facts = [_f(p, m, e, v) for e, ms in per_company.items() for m, by_p in ms.items() for p, v in by_p.items()
             if p in periods]
    return S.FactStore(facts, [{"file": "synthetic.xlsx", "period": P, "kind": "data_engine"}])


def _spec(**kw):
    base = {"id": "t", "title": "t", "theme": "t", "basis": "ytd", "unit": "pct", "so_what": "{home}",
            "formula": "cur('pbt') / cur('gwp')", "higher_is_better": True}
    return {**base, **kw}


def _margin_store():
    data = {}
    for i, e in enumerate(PEERS):
        data[e] = {"pbt": {P: 10.0 * (i + 1), PY: 5.0}, "gwp": {P: 100.0, PY: 80.0}}
    return _store(data, {P, PY})


def test_ytd_formula_value_and_rank():
    res = F.evaluate(_margin_store(), _spec(), P)
    by = {r["entity"]: r for r in res["rows"]}
    assert by["NBHI"]["value"] == pytest.approx(0.10)
    assert by["ABHI"]["value"] == pytest.approx(0.50)
    assert res["home"]["rank"] == 5 and res["best"]["entity"] == "ABHI"
    assert by["NBHI"]["ids"]                           # every figure traces to facts


def test_change_over_change_and_require_guard():
    st = _margin_store()
    res = F.evaluate(st, _spec(formula="chg('pbt') / chg('gwp') * 100", unit="rs100",
                                    require="chg('gwp') > 0"), P)
    nbhi = next(r for r in res["rows"] if r["entity"] == "NBHI")
    assert nbhi["value"] == pytest.approx((10 - 5) / (100 - 80) * 100)
    # a company whose premium fell is left out, not scored
    data = {e: {"pbt": {P: 1.0, PY: 1.0}, "gwp": {P: 90.0 if e == "STAR" else 100.0, PY: 100.0 if e == "STAR" else 80.0}}
            for e in PEERS}
    res = F.evaluate(_store(data, {P, PY}), _spec(formula="chg('pbt') / chg('gwp')", require="chg('gwp') > 0"), P)
    assert "STAR" not in {r["entity"] for r in res["rows"]}


def test_missing_inputs_skip_company_and_too_few_peers_returns_none():
    data = {e: {"pbt": {P: 1.0}, "gwp": {P: 10.0}} for e in PEERS}       # no prior-year values
    assert F.evaluate(_store(data, {P}), _spec(formula="gr('gwp')"), P) is None
    assert F.evaluate(_store(data, {P}), _spec(), P) is not None


def test_early_stage_insurers_excluded_from_peers():
    data = {e: {"pbt": {P: 1.0}, "gwp": {P: 10.0}} for e in PEERS + ["GALAXY", "NARAYANA"]}
    res = F.evaluate(_store(data, {P}), _spec(), P)
    assert {r["entity"] for r in res["rows"]} == set(PEERS)


def test_share_functions_use_all_seven_companies():
    data = {e: {"gdpi": {P: 100.0, PY: 100.0}} for e in PEERS + ["GALAXY", "NARAYANA"]}
    data["STAR"]["gdpi"][P] = 200.0
    res = F.evaluate(_store(data, {P, PY}), _spec(formula="share('gdpi') - pshare('gdpi')", unit="pp"), P)
    star = next(r for r in res["rows"] if r["entity"] == "STAR")
    assert star["value"] == pytest.approx(200 / 800 - 1 / 7)


def test_impact_is_annualised_from_ytd():
    data = {}
    for i, e in enumerate(PEERS):
        data[e] = {"health_combined_ratio": {P: 1.0 + 0.05 * i}, "health_earned_premium": {P: 600.0}}
    spec = _spec(formula="cur('health_combined_ratio')", unit="pct", higher_is_better=False,
                 fires="home > best", impact="(home - best) * home_cur('health_earned_premium') * annualise")
    res = F.evaluate(_store(data, {P}), spec, P)
    # NBHI is best (i=0) so nothing to close; make NBHI worst instead
    assert res["fired"] is False
    data["NBHI"]["health_combined_ratio"][P] = 1.20
    res = F.evaluate(_store(data, {P}), spec, P)
    assert res["fired"] and res["impact_cr"] == pytest.approx((1.20 - 1.05) * 600 * (12 / 6))   # best is now STAR at 1.05


def test_quarter_basis_differences_ytd_and_compares_to_previous_quarter():
    data = {}
    for e in PEERS:
        # claims YTD: Q1 40 -> Q2 100 (quarter = 60); EP YTD: Q1 100 -> Q2 200 (quarter = 100)
        data[e] = {"health_claims": {PQ: 40.0, P: 100.0}, "health_earned_premium": {PQ: 100.0, P: 200.0}}
    spec = _spec(basis="quarter", formula="cur('health_claims') / cur('health_earned_premium')",
                 show_prior="prior('health_claims') / prior('health_earned_premium')")
    res = F.evaluate(_store(data, {PQ, P}), spec, P)
    nbhi = next(r for r in res["rows"] if r["entity"] == "NBHI")
    assert nbhi["value"] == pytest.approx(0.60)           # 60 / 100, NOT the YTD 100 / 200
    assert nbhi["prior"] == pytest.approx(0.40)           # Q1 is its own YTD


def test_quarter_basis_rejects_non_flow_metrics_and_ratios():
    for formula in ("cur('solvency_ratio')", "cur('health_combined_ratio')", "cur('capital')"):
        with pytest.raises(F.FormulaError, match="not a flow"):
            F.validate_spec(_spec(basis="quarter", formula=formula))


@pytest.mark.parametrize("formula", ["__import__('os')", "cur('nope')", "cur(pbt)", "[1, 2]", "cur('pbt').real",
                                     "pbt + 1"])
def test_validation_rejects_unsafe_or_unknown_formulas(formula):
    with pytest.raises(F.FormulaError):
        F.validate_spec(_spec(formula=formula))
