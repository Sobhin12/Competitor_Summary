"""HTTP routes for the v2 interactive analysis layer (analysis/).

    GET  /api/analysis/meta                     periods, companies, metric catalog, LLM status
    GET  /api/analysis/highlights?period=       rule-based insights + data checks
    GET  /api/analysis/narrative?period=        LLM executive narrative over those insights
    GET  /api/analysis/news?company=&refresh=   top ranked competitor news (Parallel search + Gemini)
    GET  /api/analysis/deepdive/{company}       one competitor's full picture
    GET  /api/analysis/compare?metric=&period=  one metric across companies
    GET  /api/analysis/trend?metric=            one metric over time
    POST /api/analysis/chat                     chat agent, streamed as Server-Sent Events
    GET/PATCH/DELETE /api/analysis/chat/sessions[/{id}]   saved chats (SQLite)

Everything reads the shared FactStore, which rebuilds itself when a pipeline
run writes (or a user uploads) a Data Engine - no extra wiring into runs.py.
"""
import json
import queue
import threading

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from competitor_analysis.analysis import agent as agent_mod
from competitor_analysis.analysis import chat_store, deepdive, insights, llm, narrative, news
from competitor_analysis.analysis import store as S
from competitor_analysis.analysis import tools as T

load_dotenv()  # the chat providers' keys, when the server isn't started via the CLI

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def _store():
    store = S.get_store()
    if store.latest_quarter() is None:
        raise HTTPException(404, "No Data Engine has been produced yet - run the pipeline first.")
    return store


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except T.ToolError as e:
        raise HTTPException(400, str(e))


def _llm_status():
    try:
        c = llm.get_client()
        return {"available": True, "provider": c.name, "model": c.model}
    except llm.LLMUnavailable as e:
        return {"available": False, "provider": llm.provider_name(), "error": str(e)}


@router.get("/meta")
def meta():
    store = _store()
    return {**T.list_periods(store), **T.list_companies(store), **T.list_metrics(store), "llm": _llm_status()}


@router.get("/highlights")
def highlights(period: str | None = None):
    return _guard(insights.build, _store(), period)


@router.get("/narrative")
def narrative_route(period: str | None = None, refresh: bool = False):
    try:
        return _guard(narrative.build, _store(), period, refresh)
    except llm.LLMUnavailable as e:
        raise HTTPException(503, f"No LLM configured: {e}")
    except Exception as e:  # noqa: BLE001 - provider outage etc.; the cards still render without it
        raise HTTPException(502, f"Narrative generation failed: {str(e)[:300]}")


@router.get("/news")
async def news_route(company: str | None = None, refresh: bool = False):
    try:
        return await news.get_news(company, refresh)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except news.NewsUnavailable as e:
        raise HTTPException(503, f"News unavailable: {e}")


@router.get("/deepdive/{company}")
def deepdive_route(company: str, period: str | None = None):
    return _guard(deepdive.build, _store(), company, period)


@router.get("/compare")
def compare(metric: str, period: str | None = None, dimension: str | None = None):
    return _guard(T.compare, _store(), metric, period, None, dimension)


@router.get("/trend")
def trend(metric: str, entities: list[str] | None = Query(None), period_type: str = "annual",
          dimension: str | None = None):
    return _guard(T.trend, _store(), metric, entities, period_type, dimension)


SESSION_ID = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


class ChatRequest(BaseModel):
    """The server owns the transcript: the client names a session and sends
    only the new user message."""
    session_id: str = SESSION_ID
    message: str = Field(min_length=1, max_length=20000)
    period: str | None = None


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/chat/sessions")
def chat_sessions():
    return {"sessions": chat_store.list_sessions()}


@router.get("/chat/sessions/{session_id}")
def chat_session(session_id: str):
    s = chat_store.get_session(session_id)
    if s is None:
        raise HTTPException(404, "No such chat.")
    return s


@router.patch("/chat/sessions/{session_id}")
def chat_rename(session_id: str, req: RenameRequest):
    if not chat_store.rename(session_id, req.title):
        raise HTTPException(404, "No such chat.")
    return {"ok": True}


@router.delete("/chat/sessions/{session_id}")
def chat_delete(session_id: str):
    if not chat_store.delete(session_id):
        raise HTTPException(404, "No such chat.")
    return {"ok": True}


@router.post("/chat")
def chat(req: ChatRequest):
    """Streams: text (deltas), tool_status, chart, table, verification,
    citations, done | error. The agent runs in a worker thread and pushes
    events through a queue; this generator relays them as they arrive.

    The session (messages + working-focus state) is persisted in SQLite: the
    user message is saved before the agent runs so it survives a failure, and
    the assistant reply, with its charts/citations, once the turn finishes."""
    store = _store()
    try:
        client = llm.get_client()
    except llm.LLMUnavailable as e:
        raise HTTPException(503, f"No LLM configured: {e}")

    prior = chat_store.get_session(req.session_id)
    history = [{"role": m["role"], "content": m["content"]}
               for m in (prior["messages"] if prior else []) if m["content"] and not m.get("error")]
    history.append({"role": "user", "content": req.message})
    state = prior["state"] if prior else {}
    chat_store.append_message(req.session_id, "user", req.message)

    events = queue.Queue()

    def work():
        try:
            out = agent_mod.Agent(store=store, client=client, emit=lambda e, d: events.put((e, d))).run(
                history, req.period, state)
            chat_store.append_message(req.session_id, "assistant", out["text"], {
                "tools": out["tools"], "blocks": out["blocks"],
                "verification": {"checked": len(out["verification"]["numbers"]),
                                "unverified": out["verification"]["unverified"]},
                "citations": out["citations"]})
            chat_store.set_state(req.session_id, out["state"])
            events.put(("done", {"provider": out["provider"], "model": out["model"]}))
        except (T.ToolError, ValueError) as e:
            chat_store.append_message(req.session_id, "assistant", "", {"error": str(e)})
            events.put(("error", {"message": str(e)}))
        except Exception as e:  # noqa: BLE001 - surface provider failures to the UI
            msg = f"The model call failed: {str(e)[:300]}"
            chat_store.append_message(req.session_id, "assistant", "", {"error": msg})
            events.put(("error", {"message": msg}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            try:
                item = events.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                return
            yield _sse(*item)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
