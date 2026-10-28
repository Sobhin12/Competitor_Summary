"""Tests for the AI briefing pipeline (analysis/briefing.py, screen.py).

A scripted fake LLM drives the real investigation loop against a synthetic
FactStore, so what is under test is the code around the model: claims are
re-computed, bad numbers and anomalies are rejected, the Data Engine is never
modified, and the result is cached.
"""
import json
import time

import pytest

from competitor_analysis.analysis import briefing, llm, screen, store as S, tools as T

P, PY = "FY26-27_Q1", "FY25-26_Q1"
PEERS = ["NBHI", "STAR", "CARE", "CIGNA", "ABHI"]


def _store(extra=()):
    facts = list(extra)
    for i, e in enumerate(PEERS):
        for m, cur, pri in (("pbt", [-88, 117, -112, 15, -52][i], 0.0), ("gwp", 100.0 + 10 * i, 80.0)):
            facts += [S.Fact(P, "quarter", m, None, e, cur, "s.xlsx"), S.Fact(PY, "quarter", m, None, e, pri, "s.xlsx")]
    return S.FactStore(facts, [{"file": "s.xlsx", "period": P, "kind": "data_engine"}])


class FakeLLM:
    name, model = "fake", "fake-1"

    def __init__(self, steps):
        self.steps, self.i = steps, 0

    def complete(self, system, messages, tools, on_text=None):
        if tools:                                    # the investigator loop
            step = self.steps[min(self.i, len(self.steps) - 1)]
            self.i += 1
            return llm.Turn("", [llm.ToolCall(f"c{self.i}_{k}", n, a) for k, (n, a) in enumerate(step)])
        if system.startswith("You are a sceptical"):
            return llm.Turn(json.dumps({"verdict": "keep", "objection": "", "caveat": "Single quarter."}))
        return llm.Turn(json.dumps({"headline": "NBHI growth is the least profitable in the peer set.",
                                    "storylines": [{"title": "Growth economics",
                                                    "narrative": "Growth is not paying for itself.",
                                                    "finding_ids": ["f1"]}], "new_this_quarter": []}))


GOOD = {"title": "Growth quality", "direction": "bad", "confidence": "high", "entities": ["NBHI", "STAR"],
        "summary": "NBHI growth lost money while STAR growth made it.", "so_what": "We are the most expensive grower.",
        "claims": [{"formula": "chg('pbt') / chg('gwp') * 100", "unit": "rs100", "higher_is_better": True,
                    "require": "chg('gwp') > 0", "check": "rank >= n - 1"}]}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(briefing, "CACHE_DIR", tmp_path / "briefing")


def _run(steps, store=None):
    return briefing.build(store or _store(), P, client=FakeLLM(steps))


def test_verified_finding_reaches_the_briefing():
    out = _run([[("submit_finding", GOOD)], []])
    assert [f["id"] for f in out["findings"]] == ["f1"]
    f = out["findings"][0]
    assert f["claims"][0]["rows"] and f["claims"][0]["evidence_ids"]
    assert out["headline"].startswith("NBHI growth") and out["storylines"][0]["findings"] == ["f1"]
    assert out["stats"]["findings_kept"] == 1 and out["engine_unchanged"] is True


def test_a_claim_that_does_not_hold_is_rejected():
    bad = {**GOOD, "claims": [{**GOOD["claims"][0], "check": "rank == 1"}]}       # NBHI is not first
    out = _run([[("submit_finding", bad)], []])
    assert out["findings"] == [] and out["stats"]["rejected_by_code"] == 1
    assert "No material change" in out["headline"]


def test_numbers_not_backed_by_the_data_are_rejected():
    fabricated = {**GOOD, "summary": "NBHI growth lost ₹999 per ₹100 of new premium."}
    out = _run([[("submit_finding", fabricated)], []])
    assert out["findings"] == [] and out["stats"]["rejected_by_code"] == 1


def test_implausible_values_are_quarantined_not_headlined():
    store = _store()
    store.facts.append(S.Fact(P, "quarter", "pbt", None, "STAR", 1e9, "s.xlsx"))
    store._by_id = {f.id: f for f in store.facts}
    out = _run([[("submit_finding", GOOD)], []], store)
    assert out["findings"] == []


def test_free_formula_tool_and_dismissals_are_recorded():
    test = ("test_formula", {"formula": "chg('pbt') / chg('gwp') * 100", "unit": "rs100", "higher_is_better": True})
    out = _run([[test], [("dismiss", {"hypothesis": "CARE profit collapse", "reason": "Base effect on a small prior base."})],
                [("submit_finding", GOOD)], []])
    assert out["stats"]["hypotheses_tested"] >= 1
    assert out["ruled_out"] and out["ruled_out"][0]["hypothesis"] == "CARE profit collapse"


def test_forward_calls_are_stored_and_graded_next_period():
    watch = ("submit_watch", {"statement": "CARE returns to profit", "formula": "cur('pbt')", "unit": "cr",
                              "check": "home > 0"})
    out = _run([[watch], []])
    assert out["calls"] and out["calls"][0]["baseline_true"] is False
    graded = briefing.grade_calls(_store(), {"calls": out["calls"]}, P)
    assert graded[0]["outcome"] in ("has not happened", "came true")


def test_ai_tool_surface_is_read_only():
    assert briefing.AI_TOOLS == set(T.TOOLS) | {"test_formula", "submit_finding", "dismiss", "submit_watch"}
    assert not any(w in n for n in briefing.AI_TOOLS for w in ("write", "save", "update", "delete", "set_", "upload"))
    inv = briefing.Investigation(_store(), P, FakeLLM([]), lambda *a: None, None)
    with pytest.raises(T.ToolError):
        inv.dispatch("write_cell", {})


def test_a_modified_data_engine_discards_the_briefing(monkeypatch):
    snaps = iter([{"f": "a"}, {"f": "b"}])
    monkeypatch.setattr(briefing, "engine_snapshot", lambda: next(snaps))
    with pytest.raises(briefing.DataEngineModified):
        _run([[("submit_finding", GOOD)], []])


def test_data_engine_files_are_untouched_by_a_real_run(tmp_path, monkeypatch):
    from competitor_analysis import paths
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    f = out_dir / "Data_Engine_FY26-27_Q1_20260101_000000.xlsx"
    f.write_bytes(b"fake workbook bytes")
    monkeypatch.setattr(paths, "OUTPUT_DIR", out_dir)
    before = f.read_bytes(), f.stat().st_mtime_ns
    _run([[("submit_finding", GOOD)], []])
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before


def test_status_builds_in_background_then_serves_from_cache(monkeypatch):
    store = _store()
    monkeypatch.setattr(llm, "get_client", lambda *a, **k: FakeLLM([[("submit_finding", GOOD)], []]))
    monkeypatch.setattr(briefing, "_jobs", {})
    assert briefing.status(store, P)["status"] == "running"
    for _ in range(200):
        s = briefing.status(store, P)
        if s["status"] != "running":
            break
        time.sleep(0.05)
    assert s["status"] == "ready" and s["briefing"]["findings"]
    assert briefing.cached(store, P) is not None


def test_screen_flags_outliers_and_trend_shifts():
    facts, periods = [], [f"FY{y}-{y + 1}_Q1" for y in (22, 23, 24, 25, 26)]
    for e in PEERS:            # CARE's premium growth: steady +20% then it collapses; the others stay steady
        v = 100.0
        for k, p in enumerate(periods):
            v *= 1.2 if (e != "CARE" or k < 4) else 0.7
            facts.append(S.Fact(p, "quarter", "gwp", None, e, v, "s.xlsx"))
    st = S.FactStore(facts, [{"file": "s.xlsx", "period": periods[-1], "kind": "data_engine"}])
    kinds = {(h["entity"], h["kind"]) for h in screen.screen(st, periods[-1])["hints"]}
    assert ("CARE", "peer_outlier") in kinds or ("CARE", "outlier") in kinds


def test_a_restated_single_metric_is_rejected():
    lone = {**GOOD, "summary": "NBHI PBT is lowest.", "claims": [{"formula": "cur('pbt')", "unit": "cr",
                                                                  "higher_is_better": True, "check": "rank >= n - 1"}]}
    out = _run([[("submit_finding", lone)], []])
    assert out["findings"] == [] and out["stats"]["rejected_by_code"] == 1
