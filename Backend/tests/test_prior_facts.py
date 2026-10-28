"""Prior-period facts: source order, the facts file's write rules, and the old
workbook fallback's refusal of partial files.

Period-agnostic: nothing here names a quarter's numbers - periods come from
the configured one, and filings are stand-in byte strings (only their hash
matters to the facts file)."""
import json
import os

import openpyxl
import pytest

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis.extraction import data_engine as de
from competitor_analysis.extraction import pdf_cache
from competitor_analysis.extraction import prior_facts as pf

COMPANY = "NBHI"
TARGET = (23, "Average Claim Size", None)
KEY = pf.fact_key(*TARGET)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Facts file, output dir and downloads all under tmp_path."""
    monkeypatch.setattr(paths, "PRIOR_FACTS_DIR", tmp_path / "facts")
    monkeypatch.setattr(paths, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(paths, "DOWNLOADS_DIR", tmp_path / "downloads")
    (tmp_path / "output").mkdir()
    pf.reset_memo()
    yield tmp_path
    pf.reset_memo()


def _fake_pdf(period_fy, quarter, content=b"%PDF-fake"):
    path = cfg.expected_pdf_path(COMPANY, period_fy, quarter)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _workbook(path, rows, fy=None, quarter=None):
    """rows: [(slide, company, metric1, metric2, value)] under a header that
    labels the CUR column for the (default: prior) period."""
    col = cfg.period_column(fy or cfg.prior_fy(), quarter or cfg.QUARTER)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data Engine"
    ws.append(["Slide #", "Category", "Company", "Meric 1", "Metric 2", "Source Tab",
               "Link", col, "other", "Growth"])
    for slide, comp, m1, m2, v in rows:
        ws.append([slide, "", comp, m1, m2, "", "", v, None, None])
    wb.save(path)


# -- period switching -------------------------------------------------------

def test_temporary_period_restores_after_exception():
    before = (cfg.FY, cfg.QUARTER)
    other = cfg.prior_fy()
    with pytest.raises(RuntimeError):
        with cfg.temporary_period(other, "Q4"):
            assert (cfg.FY, cfg.QUARTER) == (other, "Q4")
            raise RuntimeError("boom")
    assert (cfg.FY, cfg.QUARTER) == before


def test_temporary_period_repoints_period_listeners():
    cur_col = de.CUR_PERIOD
    other = cfg.prior_fy()
    with cfg.temporary_period(other, cfg.QUARTER):
        assert de.CUR_PERIOD == cfg.period_column(other, cfg.QUARTER)
    assert de.CUR_PERIOD == cur_col


# -- the facts file ---------------------------------------------------------

def test_record_facts_drops_none_never_blanks_and_leaves_no_temp(sandbox):
    pdf = _fake_pdf(cfg.FY, cfg.QUARTER)
    pf.record_facts(cfg.FY, cfg.QUARTER, COMPANY, {KEY: 10.0, "x|y|": None}, pdf, "pdf")
    pf.record_facts(cfg.FY, cfg.QUARTER, COMPANY, {KEY: None}, pdf, "pdf")
    doc = json.load(open(pf.facts_path(cfg.FY, cfg.QUARTER), encoding="utf-8"))
    entry = doc["companies"][COMPANY]
    assert entry["facts"] == {KEY: 10.0}
    assert entry["_extracted"]["pdf_sha256"] == pdf_cache._content_fingerprint(pdf)[0]
    assert [f for f in os.listdir(pf.facts_path(cfg.FY, cfg.QUARTER).parent) if f.endswith(".tmp")] == []


def test_corrupt_facts_file_is_ignored_not_fatal(sandbox):
    path = pf.facts_path(cfg.FY, cfg.QUARTER)
    os.makedirs(path.parent)
    path.write_text("{not json")
    assert pf._load(cfg.FY, cfg.QUARTER)["companies"] == {}


# -- source order -----------------------------------------------------------

def _boom(*a, **k):
    raise AssertionError("must not extract")


def test_pdf_present_and_facts_stamped_with_it_skips_extraction(sandbox, monkeypatch):
    fy, q = cfg.prior_fy(), cfg.QUARTER
    pdf = _fake_pdf(fy, q)
    pf.record_facts(fy, q, COMPANY, {KEY: 5.0}, pdf, "pdf")
    monkeypatch.setattr(pf, "_extract_prior", _boom)
    facts, source = pf.get_prior_facts([COMPANY])
    assert facts[COMPANY] == {KEY: 5.0}
    assert "stamped" in source[COMPANY]


def test_changed_pdf_is_re_extracted(sandbox, monkeypatch):
    fy, q = cfg.prior_fy(), cfg.QUARTER
    pdf = _fake_pdf(fy, q)
    pf.record_facts(fy, q, COMPANY, {KEY: 5.0}, pdf, "pdf")
    _fake_pdf(fy, q, content=b"%PDF-a-different-filing")
    called = []

    def fake_extract(fy_, q_, to_extract, facts, source):
        called.append([c for c, _ in to_extract])
        facts[COMPANY][KEY] = 7.0
        source[COMPANY] = "extracted from last year's PDF"

    monkeypatch.setattr(pf, "_extract_prior", fake_extract)
    facts, _ = pf.get_prior_facts([COMPANY])
    assert called == [[COMPANY]] and facts[COMPANY][KEY] == 7.0


def test_no_pdf_uses_facts_file(sandbox, monkeypatch):
    fy, q = cfg.prior_fy(), cfg.QUARTER
    pf.record_facts(fy, q, COMPANY, {KEY: 5.0}, None, "pdf")
    monkeypatch.setattr(pf, "_extract_prior", _boom)
    facts, source = pf.get_prior_facts([COMPANY])
    assert facts[COMPANY] == {KEY: 5.0} and "no PDF" in source[COMPANY]


def test_no_prior_extract_never_touches_the_pdf(sandbox, monkeypatch):
    fy, q = cfg.prior_fy(), cfg.QUARTER
    pf.record_facts(fy, q, COMPANY, {KEY: 5.0}, _fake_pdf(fy, q), "pdf")
    monkeypatch.setattr(pf, "_extract_prior", _boom)
    facts, _ = pf.get_prior_facts([COMPANY], extract=False)
    assert facts[COMPANY] == {KEY: 5.0}


def test_nothing_anywhere_is_reported_not_substituted(sandbox):
    facts, source = pf.get_prior_facts([COMPANY])
    assert facts[COMPANY] == {} and "NOT FOUND" in source[COMPANY]


# -- workbook fallback ------------------------------------------------------

def _block(target_value, fill_others):
    rows = [(TARGET[0], COMPANY, TARGET[1], None, target_value)]
    rows += [(40, COMPANY, f"filler {i}", None, 1.0 if fill_others else None) for i in range(2)]
    return rows


def test_partial_newer_workbook_is_skipped_for_a_complete_older_one(sandbox):
    fy, q = cfg.prior_fy(), cfg.QUARTER
    out = paths.OUTPUT_DIR
    _workbook(out / f"Data_Engine_{fy}_{q}_20260101_000000.xlsx", _block(123.0, fill_others=True))
    # Newer by name, but a stopped run: 1 of 3 rows filled, and wrong-looking.
    _workbook(out / f"Data_Engine_{fy}_{q}_20260202_000000.xlsx", _block(999.0, fill_others=False))
    facts, source = pf.get_prior_facts([COMPANY], extract=False)
    assert facts[COMPANY][KEY] == 123.0
    assert "20260101" in source[COMPANY] and "fallback" in source[COMPANY]


def test_workbook_fallback_values_are_not_written_to_the_facts_file(sandbox):
    fy, q = cfg.prior_fy(), cfg.QUARTER
    _workbook(paths.OUTPUT_DIR / f"Data_Engine_{fy}_{q}_20260101_000000.xlsx", _block(123.0, True))
    pf.get_prior_facts([COMPANY], extract=False)
    assert not pf.facts_path(fy, q).exists()


def test_workbook_without_the_period_column_is_rejected(sandbox):
    fy, q = cfg.prior_fy(), cfg.QUARTER
    _workbook(paths.OUTPUT_DIR / f"Data_Engine_{fy}_{q}_20260101_000000.xlsx", _block(123.0, True),
              fy="FY01-02", quarter="Q1")
    facts, _ = pf.get_prior_facts([COMPANY], extract=False)
    assert facts[COMPANY] == {}


# -- opening on-roll ---------------------------------------------------------

def test_opening_onroll_from_facts_file_when_no_pdf(sandbox):
    prev = cfg.prior_fy()
    pf.record_facts(prev, "Q4", COMPANY, {pf.ONROLL_KEY: 8936}, None, "pdf")
    assert pf.opening_onroll(COMPANY) == 8936


def test_opening_onroll_missing_is_none_not_a_guess(sandbox):
    assert pf.opening_onroll(COMPANY) is None


def test_extract_prior_restores_period_and_gic_lookup_on_failure(sandbox, monkeypatch):
    before_period = (cfg.FY, cfg.QUARTER)
    de.set_gic_gdpi_lookup({"sentinel": (1, 2)})
    monkeypatch.setattr(pf, "_extract_company", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    facts, source = {COMPANY: {}}, {}
    pf._extract_prior(cfg.prior_fy(), cfg.QUARTER, [(COMPANY, "nowhere.pdf")], facts, source)
    assert (cfg.FY, cfg.QUARTER) == before_period
    assert de._GIC_GDPI_LOOKUP == {"sentinel": (1, 2)}
    assert facts[COMPANY] == {} and COMPANY not in source
    de.set_gic_gdpi_lookup({})
