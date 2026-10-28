"""Chat session persistence and the agent's working-focus state."""
import pytest

from competitor_analysis.analysis import agent, chat_store


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_store, "DB_FILE", tmp_path / "chat.sqlite")


def test_session_roundtrip_and_title():
    chat_store.append_message("sess-0001", "user", "Compare combined ratios")
    chat_store.append_message("sess-0001", "assistant", "Here you go",
                              {"tools": [{"name": "compare", "args": {}}], "citations": []})
    s = chat_store.get_session("sess-0001")
    assert s["title"] == "Compare combined ratios"
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"]
    assert s["messages"][1]["tools"][0]["name"] == "compare"


def test_state_rename_delete_and_ordering():
    chat_store.append_message("sess-0001", "user", "first")
    chat_store.append_message("sess-0002", "user", "second")
    assert [x["id"] for x in chat_store.list_sessions()] == ["sess-0002", "sess-0001"]
    chat_store.set_state("sess-0001", {"entities": ["STAR"]})
    assert chat_store.get_state("sess-0001") == {"entities": ["STAR"]}
    assert chat_store.rename("sess-0001", "Star deep dive")
    assert chat_store.get_session("sess-0001")["title"] == "Star deep dive"
    assert chat_store.delete("sess-0002")
    assert chat_store.get_session("sess-0002") is None
    assert not chat_store.delete("sess-0002")


def test_update_state_tracks_recent_focus():
    st = agent.update_state({}, [{"name": "compare", "args": {"metric": "combined_ratio", "period": "FY25-26_Q3"}},
                                 {"name": "company_profile", "args": {"company": "Star Health"}}])
    assert st["entities"] == ["STAR"] and st["period"] == "FY25-26_Q3"
    st = agent.update_state(st, [{"name": "trend", "args": {"entities": ["CARE", "NBHI"]}}])
    assert st["entities"][:2] == ["CARE", "NBHI"] and "STAR" in st["entities"]
    assert "Companies in focus" in agent._state_block(st)
    assert agent._state_block({}) == ""
