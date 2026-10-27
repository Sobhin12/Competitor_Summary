"""Offline tests for the chat layer: provider adapters' translation code,
the number verifier, and the agent loop driven by a scripted fake model.
No network or API keys involved."""
import json
from types import SimpleNamespace as NS

import pytest

from competitor_analysis.analysis import agent as A, llm, store as S, verify

P, PY = "FY26-27_Q1", "FY25-26_Q1"


def _f(period, metric, entity, value, dim=None):
    return S.Fact(period, "quarter", metric, dim, entity, value, "synthetic.xlsx", "https://irdai.example/nbhi.pdf")


@pytest.fixture
def store():
    facts = [_f(P, "combined_ratio", "NBHI", 1.181), _f(PY, "combined_ratio", "NBHI", 1.170),
             _f(P, "combined_ratio", "STAR", 1.001), _f(PY, "combined_ratio", "STAR", 1.022),
             _f(P, "gdpi", "NBHI", 2149.96), _f(PY, "gdpi", "NBHI", 1631.9),
             _f(P, "gdpi", "STAR", 4287.09), _f(PY, "gdpi", "STAR", 3597.06)]
    return S.FactStore(facts, [{"file": "synthetic.xlsx", "period": P, "kind": "data_engine"}])


# ---------------------------------------------------------------------------
# Databricks / OpenAI-compatible adapter
# ---------------------------------------------------------------------------

def _chunk(content=None, tool_calls=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))])


def _tc(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


def test_accumulate_stream_joins_text_and_fragmented_tool_args():
    seen = []
    turn = llm.accumulate_stream([
        _chunk("Checking "), _chunk("now."),
        _chunk(tool_calls=[_tc(0, "call_1", "compare", '{"metric": "comb')]),
        _chunk(tool_calls=[_tc(0, None, None, 'ined_ratio"}')]),
        _chunk(tool_calls=[_tc(1, "call_2", "list_periods", "")]),
        NS(choices=[]),            # usage-only trailer chunk
    ], seen.append)
    assert turn.text == "Checking now." and seen == ["Checking ", "now."]
    assert [(c.id, c.name, c.args) for c in turn.tool_calls] == [
        ("call_1", "compare", {"metric": "combined_ratio"}), ("call_2", "list_periods", {})]


def test_openai_messages_translation():
    msgs = llm.openai_messages("SYS", [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "compare", "args": {"metric": "x"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "compare", "content": "{}"},
    ])
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[2]["content"] is None and msgs[2]["tool_calls"][0]["function"]["arguments"] == '{"metric": "x"}'
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}


def test_provider_selection_prefers_databricks(monkeypatch):
    for k in ("CHAT_LLM_PROVIDER", "DATABRICKS_HOST", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET",
              "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert llm.provider_name() == "gemini"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert llm.provider_name() == "anthropic"
    monkeypatch.setenv("DATABRICKS_HOST", "adb-1.azuredatabricks.net")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    assert llm.provider_name() == "anthropic"          # secret still missing
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sec")
    assert llm.provider_name() == "databricks"
    monkeypatch.setenv("CHAT_LLM_PROVIDER", "gemini")
    assert llm.provider_name() == "gemini"


def _dbx_env(monkeypatch, host="adb-123.azuredatabricks.net/"):
    monkeypatch.setenv("DATABRICKS_HOST", host)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DATABRICKS_MODEL", "my-endpoint")
    llm.DatabricksOAuth._instances.clear()


def test_databricks_client_targets_serving_endpoints(monkeypatch):
    _dbx_env(monkeypatch)
    c = llm.DatabricksClient()
    assert str(c.client.base_url).rstrip("/") == "https://adb-123.azuredatabricks.net/serving-endpoints"
    assert c.model == "my-endpoint"
    assert c.oauth.url == "https://adb-123.azuredatabricks.net/oidc/v1/token"


def test_databricks_oauth_caches_and_refreshes(monkeypatch):
    _dbx_env(monkeypatch)
    calls = []

    def fake_post(url, auth, data, timeout):
        calls.append((url, auth, data))
        return NS(status_code=200, text="", json=lambda: {"access_token": f"tok{len(calls)}", "expires_in": 3600})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    oauth = llm.DatabricksClient().oauth
    assert oauth.get() == "tok1" and oauth.get() == "tok1"          # cached
    assert calls[0][1] == ("cid", "secret")
    assert calls[0][2] == {"grant_type": "client_credentials", "scope": "all-apis"}
    oauth.expires_at = 0                                              # expired
    assert oauth.get() == "tok2"


def test_databricks_oauth_failure_is_reported(monkeypatch):
    _dbx_env(monkeypatch)
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: NS(status_code=401, text="invalid_client"))
    with pytest.raises(llm.LLMUnavailable, match="401"):
        llm.DatabricksClient().oauth.get()


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

def test_verify_accepts_tool_numbers_and_flags_invented_ones():
    res = [{"rows": [{"value": 1.001, "display": "100.1%", "ids": ["a"]}, {"value": 4287.09, "ids": ["b"]}]}]
    out = verify.verify("Star at 100.1% in Q1 FY26-27 with ₹4,287 Cr GDPI, rank 1; margin 12.5%.", res)
    by = {n["text"]: n for n in out["numbers"]}
    assert by["100.1%"]["verified"] and by["100.1%"]["ids"] == ["a"]
    assert by["₹4,287 Cr"]["verified"]
    assert out["unverified"] == ["12.5%"]


# ---------------------------------------------------------------------------
# Agent loop with a scripted model
# ---------------------------------------------------------------------------

class ScriptedClient:
    name, model = "fake", "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def complete(self, system, messages, tools, on_text=None):
        self.seen.append(messages[-1])
        turn = self.turns.pop(0)
        if turn.text and on_text:
            on_text(turn.text)
        return turn


def test_agent_runs_tools_charts_and_verifies(store):
    chart = {"type": "bar", "title": "Combined ratio", "unit": "pct", "categories": ["NBHI", "STAR"],
             "series": [{"name": "Q1 FY26-27", "values": [1.181, 1.001]}]}
    client = ScriptedClient([
        llm.Turn("", [llm.ToolCall("c1", "compare", {"metric": "combined_ratio"})]),
        llm.Turn("", [llm.ToolCall("c2", "render_chart", chart),
                      llm.ToolCall("c3", "calculate", {"expression": "1.181 - 1.001"})]),
        llm.Turn("STAR leads at 100.1% vs NBHI 118.1%; a gap of 18.0 pp. NBHI is 99.9% sure."),
    ])
    events = []
    out = A.Agent(store=store, client=client, emit=lambda e, d: events.append((e, d))).run(
        [{"role": "user", "content": "Compare combined ratios"}])
    kinds = [e for e, _ in events]
    assert "chart" in kinds and kinds[-2:] == ["verification", "citations"]
    assert out["verification"]["unverified"] == ["99.9%"]
    assert {c["entity"] for c in out["citations"]} >= {"STAR", "NBHI"}
    # The calculate result went back to the model as a tool message.
    tool_msgs = [m for m in client.seen if m["role"] == "tool"]
    assert json.loads(tool_msgs[-1]["content"])["result"] == pytest.approx(0.18)


def test_agent_rejects_chart_values_not_from_tools(store):
    bad = {"type": "bar", "title": "x", "unit": "cr", "categories": ["NBHI"],
           "series": [{"name": "s", "values": [123456.0]}]}
    client = ScriptedClient([
        llm.Turn("", [llm.ToolCall("c1", "render_chart", bad)]),
        llm.Turn("Sorry."),
    ])
    out = A.Agent(store=store, client=client).run([{"role": "user", "content": "chart"}])
    assert out["blocks"] == []
    err = json.loads([m for m in client.seen if m["role"] == "tool"][0]["content"])
    assert "not in any tool result" in err["error"]


def test_calculate_rejects_operands_not_from_tools(store):
    client = ScriptedClient([
        llm.Turn("", [llm.ToolCall("c1", "calculate", {"expression": "777.7 / 2"})]),
        llm.Turn("ok"),
    ])
    A.Agent(store=store, client=client).run([{"role": "user", "content": "x"}])
    err = json.loads([m for m in client.seen if m["role"] == "tool"][0]["content"])
    assert "error" in err
