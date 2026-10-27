"""Contract tests for /api/analysis/* - shapes and status codes the React
views consume. Uses the Data Engines on disk (skips without any) and a
scripted fake LLM, so no network is involved."""
import glob
import json
import os

import pytest
from fastapi.testclient import TestClient

from competitor_analysis import paths
from competitor_analysis.analysis import llm
from competitor_analysis.api import analysis_routes
from competitor_analysis.api.main import app

pytestmark = pytest.mark.skipif(
    not glob.glob(os.path.join(str(paths.OUTPUT_DIR), "Data_Engine_*_Q[1-4]_*.xlsx")),
    reason="no Data Engine workbooks in artifacts/output")

client = TestClient(app)


class FakeLLM:
    name, model = "fake", "scripted"

    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, system, messages, tools, on_text=None):
        turn = self.turns.pop(0)
        if turn.text and on_text:
            on_text(turn.text)
        return turn


def _sse_events(body):
    out = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_meta():
    r = client.get("/api/analysis/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["latest_quarter"] and body["companies"] and body["metrics"]
    assert "available" in body["llm"]


def test_highlights_and_deepdive():
    h = client.get("/api/analysis/highlights").json()
    assert h["insights"] and {"headline", "drivers", "evidence", "theme"} <= set(h["insights"][0])
    d = client.get("/api/analysis/deepdive/STAR").json()
    assert d["company"] == "STAR" and d["kpis"] and d["sections"]


def test_bad_arguments_are_400():
    assert client.get("/api/analysis/compare", params={"metric": "not a metric"}).status_code == 400
    assert client.get("/api/analysis/deepdive/SAHI").status_code == 400
    assert client.get("/api/analysis/highlights", params={"period": "last spring"}).status_code == 400


def test_chat_streams_events(monkeypatch):
    fake = FakeLLM([
        llm.Turn("", [llm.ToolCall("c1", "compare", {"metric": "combined_ratio"})]),
        llm.Turn("Done."),
    ])
    monkeypatch.setattr(analysis_routes.llm, "get_client", lambda: fake)
    r = client.post("/api/analysis/chat", json={"messages": [{"role": "user", "content": "combined ratios?"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    kinds = [e for e, _ in _sse_events(r.text)]
    assert kinds[0] == "tool_status" and "text" in kinds and kinds[-1] == "done"


def test_chat_without_llm_is_503(monkeypatch):
    def unavailable():
        raise llm.LLMUnavailable("no key")
    monkeypatch.setattr(analysis_routes.llm, "get_client", unavailable)
    r = client.post("/api/analysis/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
