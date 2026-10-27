"""Direct (non-model) readers and the quick-scan page gate.

Synthetic tables only - no filings, no network - so these run anywhere. The
real-filing agreement with the model's answers was checked when the readers
were written; what is pinned here is the behaviour that makes them safe: they
return only what they can resolve, never half an answer.
"""
import pytest

from competitor_analysis import config as cfg
from competitor_analysis.extraction import direct_reads, gemini as g, pdf_cache


def _doc(form, tables, text=""):
    return {"pages": [{"page_number": 1, "forms_detected": [form], "tables": tables,
                       "text": text}]}


def _year_labels():
    cur, prior = cfg.calendar_mapping()["year"], cfg.calendar_mapping()["year"] - 1
    return cur, prior


def test_nl34_reads_both_periods_from_headers():
    cur, prior = _year_labels()
    table = [
        ["S.No", "State", "Total", ""],
        ["", "", f"Up to the quarter ended March 31, {cur}", f"Up to the quarter ended March 31, {prior}"],
        ["1", "Goa", "1 ,200", "9 00"],
        ["2", "Govt. of NCT of Delhi", "5,000", "4,000"],
    ]
    out = direct_reads.read_nl34(_doc("NL-34", [table]))
    assert out["state_Goa"]["fy26_q3"] == 1200 and out["state_Goa"]["fy25_q3"] == 900
    assert out["state_Delhi"]["fy26_q3"] == 5000


def test_nl34_current_year_only_schedule_gives_null_prior():
    cur, _ = _year_labels()
    table = [
        ["S.No", "State", "Total"],
        ["", "", f"Up to the quarter ended March 31, {cur}"],
        ["1", "Goa", "1,200"],
    ]
    out = direct_reads.read_nl34(_doc("NL-34", [table]))
    assert out["state_Goa"]["fy26_q3"] == 1200 and out["state_Goa"]["fy25_q3"] is None


def test_nl34_blank_row_is_left_to_the_model_not_turned_into_zero():
    cur, prior = _year_labels()
    table = [
        ["S.No", "State", "Total", ""],
        ["", "", f"Up to the quarter ended March 31, {cur}", f"Up to the quarter ended March 31, {prior}"],
        ["1", "Goa", "", ""],
    ]
    assert "state_Goa" not in direct_reads.read_nl34(_doc("NL-34", [table]))


def test_nl34_exact_label_match_only():
    cur, prior = _year_labels()
    table = [
        ["S.No", "State", "Total", ""],
        ["", "", f"Up to the quarter ended March 31, {cur}", f"Up to the quarter ended March 31, {prior}"],
        ["1", "Goa and Daman Region", "7", "7"],
    ]
    assert "state_Goa" not in direct_reads.read_nl34(_doc("NL-34", [table]))


def test_nl34_others_reported_not_found_only_when_no_such_row():
    cur, _ = _year_labels()
    base = [["S.No", "State", "Total"],
            ["", "", f"Up to the quarter ended March 31, {cur}"],
            ["1", "Goa", "5"]]
    assert direct_reads.read_nl34(_doc("NL-34", [base]))["state_Others"]["found"] is False
    with_others = base + [["2", "Others", "9"]]
    assert "state_Others" not in direct_reads.read_nl34(_doc("NL-34", [with_others]))


def test_read_all_isolates_a_failing_reader(monkeypatch):
    def boom(doc):
        raise RuntimeError("layout surprise")

    monkeypatch.setattr(direct_reads, "READERS", (boom, lambda d: {"x": {"found": True}}))
    assert direct_reads.read_all({}) == {"x": {"found": True}}
    assert direct_reads.read_all({}, wanted_keys={"y"}) == {}


def test_split_direct_sends_only_unresolved_metrics_ordered_by_form(monkeypatch):
    specs = [
        {"key": "a", "forms": ["NL-7"], "description": "", "kind": "money", "rows": []},
        {"key": "b", "forms": ["NL-34"], "description": "", "kind": "money", "rows": []},
        {"key": "c", "forms": ["NL-12"], "description": "", "kind": "money", "rows": []},
    ]
    monkeypatch.setattr(pdf_cache, "get_company_json", lambda p: {})
    monkeypatch.setattr(direct_reads, "read_all",
                        lambda doc, wanted: {"b": {"found": True}})
    direct, rest = g.split_direct("x.pdf", specs)
    assert set(direct) == {"b"}
    assert [m["key"] for m in rest] == ["c", "a"]  # NL-12 before NL-7


def test_split_direct_falls_back_to_the_model_when_reads_blow_up(monkeypatch):
    specs = [{"key": "a", "forms": ["NL-7"], "description": "", "kind": "money", "rows": []}]

    def broken(path):
        raise OSError("unreadable")

    monkeypatch.setattr(pdf_cache, "get_company_json", broken)
    direct, rest = g.split_direct("x.pdf", specs)
    assert direct == {} and [m["key"] for m in rest] == ["a"]


def test_direct_reads_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(g, "USE_DIRECT_READS", False)
    specs = [{"key": "a", "forms": ["NL-34"], "description": "", "kind": "money", "rows": []}]
    direct, rest = g.split_direct("x.pdf", specs)
    assert direct == {} and len(rest) == 1


# --- quick-scan page gate -------------------------------------------------

def test_gate_covers_every_form_pattern_and_the_whole_document_selectors():
    # Every FORM_PATTERNS entry must trip the gate on its own header text...
    samples = {"NL-1": "FORM NL-1-B-RA", "NL-4": "FORM NL-4 PREMIUM", "NL-12": "FORM NL-12A",
               "NL-29": "NL-29 DETAILS REGARDING DEBT", "NL-41": "FORM NL-41 OFFICES"}
    for text in samples.values():
        assert any(p.search(text) for p in pdf_cache._GATE_PATTERNS), text
    # ...and so must the pages the consumers find by their own regexes.
    for text in ("NL 14 fixed assets", "GREIVANCE DISPOSAL", "see NL-41"):
        assert any(p.search(text) for p in pdf_cache._GATE_PATTERNS), text
    # A page with no schedule is skipped.
    assert not any(p.search("Statement of directors") for p in pdf_cache._GATE_PATTERNS)


def test_skipped_pages_stay_as_placeholders_and_gated_cache_requires_matching_gate(tmp_path, monkeypatch):
    import json
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4\nx\n")
    monkeypatch.setattr(pdf_cache, "CACHE_ROOT", str(tmp_path / "cache"))
    sha, size = pdf_cache._content_fingerprint(str(pdf))
    path = pdf_cache._cache_path("T")
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"pdf_sha256": sha, "pdf_size": size, "pages": [], "gate_signature": "stale"}
    json.dump(entry, open(path, "w"))
    assert pdf_cache._read_cached(path, str(pdf)) is None          # built with another gate
    entry["gate_signature"] = pdf_cache.GATE_SIGNATURE
    json.dump(entry, open(path, "w"))
    assert pdf_cache._read_cached(path, str(pdf)) is not None      # same gate
    del entry["gate_signature"]
    json.dump(entry, open(path, "w"))
    assert pdf_cache._read_cached(path, str(pdf)) is not None      # a full parse satisfies any gate


def test_company_for_path_resolves_by_filename_not_basename(monkeypatch):
    monkeypatch.setattr(pdf_cache, "_PDF_TO_COMPANY", {})
    assert pdf_cache._company_for_path("/x/y/Aditya_Birla_Health_Insurance.pdf") == "ABHI"


# --- NL-29 / NL-41 / NL-7 / NL-3 / NL-12 ----------------------------------

def _dates():
    cur, prior = _year_labels()
    return f"As at March 31, {cur}", f"As at March 31, {prior}"


def test_nl29_reads_book_value_percentages_and_sums_rated_below_a():
    d_cur, d_prior = _dates()
    table = [
        ["", "Market Value", "", "", "", "Book Value", "", "", ""],
        ["", d_cur, "as % of total for this class", d_prior, "as % of total for this class",
         d_cur, "as % of total for this class", d_prior, "as % of total for this class"],
        ["Break down by credit rating", "", "", "", "", "", "", "", ""],
        ["AAA rated", "1", "50%", "1", "40%", "1", "60%", "1", "55%"],
        ["Rated below A but above B", "1", "1%", "1", "1%", "1", "2%", "1", "1%"],
        ["Rated Below B", "-", "-", "-", "-", "1", "1%", "1", "2%"],
        ["Any other (Sovereign)", "1", "49%", "1", "59%", "1", "37%", "1", "42%"],
        ["Breakdown By Residual Maturity", "", "", "", "", "", "", "", ""],
        ["Up to 1 year", "1", "9%", "1", "9%", "1", "10%", "1", "11%"],
        ["Above 10 years", "1", "9%", "1", "9%", "1", "12%", "1", "13%"],
    ]
    out = direct_reads.read_nl29(_doc("NL-29", [table]))
    assert (out["debt_rating_AAA rated"]["fy26_q3"], out["debt_rating_AAA rated"]["fy25_q3"]) == (60, 55)
    assert (out["debt_rating_Sovereign"]["fy26_q3"], out["debt_rating_Sovereign"]["fy25_q3"]) == (37, 42)
    assert (out["debt_rating_Rated below A"]["fy26_q3"], out["debt_rating_Rated below A"]["fy25_q3"]) == (3, 3)
    assert out["debt_maturity_Up to 1 year"]["fy26_q3"] == 10
    assert "debt_maturity_More than 1 year and upto 3 years" not in out  # row absent: model's call


def test_nl29_without_a_prior_year_book_is_left_to_the_model():
    d_cur, d_prior = _dates()
    table = [
        ["", "Book Value", "", "", ""],
        ["", d_cur, "as % of total for this class", d_prior, "as % of total for this class"],
        ["AAA rated", "1", "100%", "-", "-"],
        ["Breakdown By Residual Maturity", "", "", "", ""],
        ["Up to 1 year", "1", "100%", "-", "-"],
    ]
    assert direct_reads.read_nl29(_doc("NL-29", [table])) == {}


def test_nl41_packed_and_sub_row_layouts():
    packed = [
        ["11", "No. of Employees (a) On-roll (b) Off-roll (c) Total", "", "(a) 10,191 (b) 0 (c) 10,191"],
        ["12", "No. of Insurance Agents and Intermediaries (a) Individual Agents (b) Corporate "
               "Agents-Banks (e) Web Aggregators", "", "(a) 2,38,517 (b) 28 (e) 10"],
        ["6", "No of branches at the end of the year", "", "210"],
    ]
    out = direct_reads.read_nl41(_doc("NL-41", [packed]))
    assert out["employees_onroll"]["fy26_q3"] == 10191
    assert out["agents_individual"]["fy26_q3"] == 238517
    assert out["intermediary_Individual Agents"]["fy26_q3"] == 238517
    assert out["intermediary_WA"]["fy26_q3"] == 10
    assert out["offices_count"]["fy26_q3"] == 210
    sub_rows = [["12", "No. of Insurance Agents and Intermediaries", "", ""],
                ["", "(a) Individual Agents", "", "4 ,11,408"],
                ["", "(e) Web Aggregators", "", "9"]]
    out = direct_reads.read_nl41(_doc("NL-41", [sub_rows]))
    assert out["agents_individual"]["fy26_q3"] == 411408 and out["intermediary_WA"]["fy26_q3"] == 9


def test_nl41_dash_inside_a_word_is_not_a_zero():
    table = [["11", "No. of Employees (a) On-roll (b) Off-roll", "", "(a) On-roll : 18566 (b) 3"]]
    assert direct_reads.read_nl41(_doc("NL-41", [table]))["employees_onroll"]["fy26_q3"] == 18566


def test_nl7_reads_rows_and_rejects_an_ambiguous_label():
    cur, prior = _year_labels()
    header = [["S.No", "Particulars", "Total", ""],
              ["", "", f"Up to the quarter ended March 31, {cur}", f"Up to the quarter ended March 31, {prior}"]]
    ok = header + [["4", "Rents, rates & taxes", "2,866", "2,826"]]
    out = direct_reads.read_nl7(_doc("NL-7", [ok]))
    assert out["rent_expense"]["fy26_q3"] == 2866 and out["rent_expense"]["fy25_q3"] == 2826
    twice = ok + [["", "Rents, rates & taxes", "1", "1"]]
    assert "rent_expense" not in direct_reads.read_nl7(_doc("NL-7", [twice]))
    zero_it = header + [["15", "Information Technology Expenses", "-", "-"]]
    assert "it_spend" not in direct_reads.read_nl7(_doc("NL-7", [zero_it]))


def test_nl3_reads_snapshot_columns():
    d_cur, d_prior = _dates()
    table = [["Particulars", "Ref", d_cur, d_prior],
             ["SHARE CAPITAL", "NL-8", "1,84,746", "1,82,703"],
             ["RESERVES AND SURPLUS", "NL-10", "1,95,918", "1,94,974"],
             ["DEBIT BALANCE IN PROFIT & LOSS", "", "58,767", "71,845"]]
    out = direct_reads.read_nl3(_doc("NL-3", [table]))
    assert out["capital"]["fy26_q3"] == 184746 and out["capital"]["fy25_q3"] == 182703
    assert out["bs_debit_balance_pl"]["fy25_q3"] == 71845


def test_nl12_picks_each_group_column_from_the_grand_total_row():
    d_cur, d_prior = _dates()
    table = [["", "", "Shareholders", "", "Policyholders", "", "Total", ""],
             ["S. No", "Particulars", d_cur, d_prior, d_cur, d_prior, d_cur, d_prior],
             ["", "GRAND TOTAL", "3,60,806", "3,61,992", "6,06,159", "4,55,516", "9,66,965", "8,17,508"]]
    out = direct_reads.read_nl12(_doc("NL-12", [table]))
    assert out["aum_total"]["fy26_q3"] == 966965 and out["aum_total"]["fy25_q3"] == 817508
    assert out["aum_shareholders"]["fy26_q3"] == 360806
    assert out["aum_policyholders"]["fy25_q3"] == 455516


def test_nl6_joins_a_headerless_channel_table_to_the_header_above():
    cur, prior = _year_labels()
    header_table = [["Particulars", "Health", "", "Grand Total", ""],
                    ["", f"For the quarter ended March 31, {cur}", f"Up to the quarter ended March 31, {cur}",
                     f"For the quarter ended March 31, {cur}", f"Up to the quarter ended March 31, {cur}"],
                    ["Gross Commission", "1", "2", "1", "2"]]
    prior_table = [["Particulars", "Health", "", "Grand Total", ""],
                   ["", f"For the quarter ended March 31, {prior}", f"Up to the quarter ended March 31, {prior}",
                    f"For the quarter ended March 31, {prior}", f"Up to the quarter ended March 31, {prior}"],
                   ["Gross Commission", "1", "2", "1", "2"]]
    chans_cur = [["Individual Agents", "7", "100", "7", "1,000"]]
    chans_prior = [["Individual Agents", "6", "90", "6", "900"]]
    doc = _doc("NL-6", [header_table, chans_cur, prior_table, chans_prior])
    out = direct_reads.read_nl6(doc)
    assert out["commission_ch_Individual Agents"]["fy26_q3"] == 1000
    assert out["commission_ch_Individual Agents"]["fy25_q3"] == 900
