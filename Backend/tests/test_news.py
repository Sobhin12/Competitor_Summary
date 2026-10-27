"""News feed: caching, isolation and fallbacks. No network - search and Gemini are stubbed."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from competitor_analysis.analysis import news


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(news, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(news.s3, "upload_file", lambda p: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test")


def _item(n, date="2026-10-%02d"):
    return {"url": f"https://x.test/{n}", "title": f"t{n}", "publish_date": date % n if "%" in date else date,
            "excerpt": "e", "source": "x.test"}


def _run(coro):
    return asyncio.run(coro)


def _stub(monkeypatch, items, calls):
    async def search(company):
        calls.append(company)
        return list(items)

    async def summarise(company, its):
        return [i | {"summary": "s", "tag": "Product"} for i in its if i["title"] != "t2"]

    monkeypatch.setattr(news, "_search", search)
    monkeypatch.setattr(news, "_summarise", summarise)


def test_filters_sorts_caps_and_caches(monkeypatch):
    calls = []
    _stub(monkeypatch, [_item(n) for n in range(1, 13)], calls)

    first = _run(news.get_news("STAR"))["companies"][0]
    assert first["cached"] is False and first["summarised"] is True
    dates = [i["publish_date"] for i in first["items"]]
    assert len(dates) == news.MAX_ITEMS and dates == sorted(dates, reverse=True)
    assert all(i["title"] != "t2" for i in first["items"])  # irrelevant dropped

    second = _run(news.get_news("STAR"))["companies"][0]
    assert second["cached"] is True and calls == ["STAR"]

    _run(news.get_news("STAR", refresh=True))
    assert calls == ["STAR", "STAR"]


def test_ttl_expiry_refetches(monkeypatch):
    calls = []
    _stub(monkeypatch, [_item(1)], calls)
    _run(news.get_news("CARE"))
    path = news._cache_path("CARE")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=news.NEWS_TTL_HOURS + 1)).isoformat()
    path.write_text(json.dumps(entry), encoding="utf-8")
    _run(news.get_news("CARE"))
    assert calls == ["CARE", "CARE"]


def test_one_company_failure_is_isolated(monkeypatch):
    async def search(company):
        if company == "STAR":
            raise RuntimeError("boom")
        return [_item(1)]

    async def summarise(company, its):
        return its

    monkeypatch.setattr(news, "_search", search)
    monkeypatch.setattr(news, "_summarise", summarise)
    by = {c["company"]: c for c in _run(news.get_news())["companies"]}
    assert len(by) == 7
    assert by["STAR"]["error"] == "boom" and by["STAR"]["items"] == []
    assert by["NBHI"]["items"]


def test_gemini_failure_keeps_headlines(monkeypatch):
    async def search(company):
        return [_item(1)]

    async def summarise(company, its):
        raise RuntimeError("quota")

    monkeypatch.setattr(news, "_search", search)
    monkeypatch.setattr(news, "_summarise", summarise)
    entry = _run(news.get_news("ABHI"))["companies"][0]
    assert entry["summarised"] is False and len(entry["items"]) == 1


def test_search_dedupes_and_drops_own_filings(monkeypatch):
    def r(url):
        return SimpleNamespace(url=url, title="T", publish_date="2026-10-01", excerpts=["x" * 900])

    results = [r("https://www.site.test/a"), r("https://www.site.test/a"), r("https://insurer.test/q3.pdf")]

    class FakeClient:
        async def search(self, **kw):
            assert kw["advanced_settings"]["source_policy"]["after_date"]
            return SimpleNamespace(results=results)

    monkeypatch.setattr(news, "_search_client", lambda: FakeClient())
    items = _run(news._search("NBHI"))
    assert [i["url"] for i in items] == ["https://www.site.test/a"]
    assert items[0]["source"] == "site.test" and len(items[0]["excerpt"]) == news.EXCERPT_CHARS


def test_missing_parallel_key_raises(monkeypatch):
    monkeypatch.setattr(news, "_client", None)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    with pytest.raises(news.NewsUnavailable):
        _run(news.get_news("STAR"))


def test_route_errors(monkeypatch):
    from competitor_analysis.api.main import app
    monkeypatch.setattr(news, "_client", None)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    c = TestClient(app)
    assert c.get("/api/analysis/news?company=NOPE").status_code == 400
    assert c.get("/api/analysis/news?company=STAR").status_code == 503
