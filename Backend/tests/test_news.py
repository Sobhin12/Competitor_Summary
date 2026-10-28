"""News feed: ranking, caching, isolation and fallbacks. No network - search and Gemini are stubbed."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from competitor_analysis.analysis import news

TODAY = date(2026, 10, 27)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(news, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(news.s3, "upload_file", lambda p: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test")


def _run(coro):
    return asyncio.run(coro)


def _item(name, importance=3, days_ago=1, url=None):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    return {"url": url or f"https://x.test/{name}", "title": name, "publish_date": d, "excerpt": "an excerpt",
            "source": "x.test", "summary": f"summary {name}", "tag": "Product", "importance": importance}


def _entry(company, *items):
    return {"company": company, "items": list(items), "summarised": True, "cached": False,
            "fetched_at": "2026-10-27T00:00:00+00:00"}


def _stub(monkeypatch, per_company, calls=None):
    async def search(company):
        if calls is not None:
            calls.append(company)
        return [dict(i) for i in per_company.get(company, [])]

    async def summarise(company, items):
        return items

    monkeypatch.setattr(news, "_search", search)
    monkeypatch.setattr(news, "_summarise", summarise)


def test_niva_bupa_is_not_tracked():
    assert "NBHI" not in news.COMPANY_ORDER and len(news.COMPANY_ORDER) == 6


def test_rank_orders_by_importance_then_recency():
    today = date.today()
    entries = [_entry("STAR", _item("minor-new", 2, 0), _item("big-old", 5, 20)),
               _entry("CARE", _item("mid", 4, 1))]
    titles = [i["title"] for i in news.rank(entries, limit=8, per_company=3)]
    # scores: big-old 5 + 0.33, mid 4 + 0.97, minor-new 2 + 1.0
    assert titles == ["big-old", "mid", "minor-new"]


def test_rank_caps_per_company_and_limit():
    star = [_item(f"s{n}", 5, n) for n in range(6)]
    care = [_item(f"c{n}", 1, n) for n in range(6)]
    out = news.rank([_entry("STAR", *star), _entry("CARE", *care)], limit=8, per_company=3)
    comps = [i["companies"][0] for i in out]
    assert comps.count("STAR") == 3 and comps.count("CARE") == 3 and len(out) == 6
    assert len(news.rank([_entry("STAR", *star)], limit=2, per_company=3)) == 2


def test_rank_merges_same_url_across_companies():
    shared = "https://x.test/shared"
    out = news.rank([_entry("STAR", _item("shared", 5, 1, shared)), _entry("CARE", _item("shared", 5, 1, shared))],
                    limit=8, per_company=3)
    assert len(out) == 1 and out[0]["companies"] == ["STAR", "CARE"]


def test_rank_description_falls_back_to_excerpt():
    plain = {"url": "https://x.test/p", "title": "p", "publish_date": None, "excerpt": "e" * 500, "source": "x.test"}
    out = news.rank([_entry("STAR", plain)], limit=8, per_company=3)
    assert out[0]["description"] == "e" * news.DESCRIPTION_CHARS and out[0]["tag"] is None


def test_get_news_pools_ranks_and_caches(monkeypatch):
    calls = []
    _stub(monkeypatch, {c: [_item(f"{c}-{n}", 3, n) for n in range(5)] for c in news.COMPANY_ORDER}, calls)

    first = _run(news.get_news())
    assert len(first["items"]) == news.NEWS_TOP_N == 8 and first["cached"] is False
    assert first["errors"] == [] and sorted(calls) == sorted(news.COMPANY_ORDER)
    assert all(sum(c in i["companies"] for i in first["items"]) <= 3 for c in news.COMPANY_ORDER)

    second = _run(news.get_news())
    assert second["cached"] is True and len(calls) == 6

    _run(news.get_news(refresh=True))
    assert len(calls) == 12


def test_limit_setting(monkeypatch):
    _stub(monkeypatch, {c: [_item(f"{c}-{n}", 3, n) for n in range(5)] for c in news.COMPANY_ORDER})
    monkeypatch.setattr(news, "NEWS_TOP_N", 4)
    assert len(_run(news.get_news())["items"]) == 4


def test_ttl_expiry_refetches(monkeypatch):
    calls = []
    _stub(monkeypatch, {"CARE": [_item("a")]}, calls)
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
        return [_item(f"{company}-a")]

    async def summarise(company, its):
        return its

    monkeypatch.setattr(news, "_search", search)
    monkeypatch.setattr(news, "_summarise", summarise)
    out = _run(news.get_news())
    assert out["errors"] == [{"company": "STAR", "error": "boom"}]
    assert len(out["items"]) == 5


def test_gemini_failure_keeps_headlines(monkeypatch):
    async def search(company):
        return [{"url": "https://x.test/1", "title": "t", "publish_date": "2026-10-20", "excerpt": "ex",
                 "source": "x.test"}]

    async def summarise(company, its):
        raise RuntimeError("quota")

    monkeypatch.setattr(news, "_search", search)
    monkeypatch.setattr(news, "_summarise", summarise)
    out = _run(news.get_news("ABHI"))
    assert out["summarised"] is False and out["items"][0]["description"] == "ex"


def test_search_dedupes_and_drops_own_filings(monkeypatch):
    def r(url):
        return SimpleNamespace(url=url, title="T", publish_date="2026-10-01", excerpts=["x" * 900])

    results = [r("https://www.site.test/a"), r("https://www.site.test/a"), r("https://insurer.test/q3.pdf")]

    class FakeClient:
        async def search(self, **kw):
            assert kw["advanced_settings"]["source_policy"]["after_date"]
            return SimpleNamespace(results=results)

    monkeypatch.setattr(news, "_search_client", lambda: FakeClient())
    items = _run(news._search("STAR"))
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
    assert c.get("/api/analysis/news?company=NBHI").status_code == 400
    assert c.get("/api/analysis/news").status_code == 503


def test_articles_older_than_the_window_are_dropped(monkeypatch):
    _stub(monkeypatch, {"CARE": [_item("fresh", days_ago=3), _item("stale", days_ago=news.LOOKBACK_DAYS + 5)]})
    titles = [i["title"] for i in _run(news.get_news("CARE"))["items"]]
    assert titles == ["fresh"]


def test_summarise_drops_irrelevant_and_duplicates(monkeypatch):
    items = [{"url": f"https://x.test/{n}", "title": n, "publish_date": None, "excerpt": "e", "source": "x.test"}
             for n in "abcd"]
    verdicts = [
        {"url": "https://x.test/a", "relevant": True, "summary": "A", "tag": "Product", "importance": 9},
        {"url": "https://x.test/b", "relevant": False, "summary": "B", "tag": "Other", "importance": 2},
        {"url": "https://x.test/c", "relevant": True, "summary": "C", "tag": "Product", "importance": 3,
         "duplicate_of": "https://x.test/a"},
        {"url": "https://x.test/d", "relevant": True, "summary": "D", "tag": "Financial", "importance": 0},
    ]
    resp = SimpleNamespace(text=json.dumps({"articles": verdicts}))
    fake = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))
    monkeypatch.setattr(news.gemini, "client", lambda: fake)
    monkeypatch.setattr(news, "_limiter", SimpleNamespace(acquire=lambda: asyncio.sleep(0)))
    out = _run(news._summarise("STAR", items))
    assert [(i["title"], i["importance"]) for i in out] == [("a", 5), ("d", 1)]  # clamped to 1-5
