"""HTTP routes for the v2 interactive analysis layer (analysis/).

    GET  /api/analysis/meta                     periods, companies, metric catalog, LLM status
    GET  /api/analysis/highlights?period=       rule-based insights + data checks
    GET  /api/analysis/narrative?period=        LLM executive narrative over those insights
    GET  /api/analysis/news?company=&refresh=   recent news per competitor (Parallel search + Gemini tags)
    GET  /api/analysis/deepdive/{company}       one competitor's full picture
    GET  /api/analysis/compare?metric=&period=  one metric across companies
    GET  /api/analysis/trend?metric=            one metric over time
    POST /api/analysis/chat                     chat agent, streamed as Server-Sent Events

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
from competitor_analysis.analysis import deepdive, insights, llm, narrative, news
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


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=20000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    period: str | None = None


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
def chat(req: ChatRequest):
    """Streams: text (deltas), tool_status, chart, table, verification,
    citations, done | error. The agent runs in a worker thread and pushes
    events through a queue; this generator relays them as they arrive."""
    store = _store()
    try:
        client = llm.get_client()
    except llm.LLMUnavailable as e:
        raise HTTPException(503, f"No LLM configured: {e}")

    events = queue.Queue()
    history = [m.model_dump() for m in req.messages]

    def work():
        try:
            out = agent_mod.Agent(store=store, client=client, emit=lambda e, d: events.put((e, d))).run(
                history, req.period)
            events.put(("done", {"provider": out["provider"], "model": out["model"]}))
        except (T.ToolError, ValueError) as e:
            events.put(("error", {"message": str(e)}))
        except Exception as e:  # noqa: BLE001 - surface provider failures to the UI
            events.put(("error", {"message": f"The model call failed: {str(e)[:300]}"}))
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
