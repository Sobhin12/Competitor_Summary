"""
Top competitor news for the dashboard's News tab.

Phase 1 of a fetch is a Parallel Search API call per competitor insurer (last
30 days; Niva Bupa is excluded - this is competitor intelligence); phase 2 is
one Gemini call per insurer that drops off-topic hits and adds a short
summary, a category tag and a 1-5 importance score. The articles are then
pooled across insurers and ranked, and the top NEWS_TOP_N are returned. If
Gemini is unavailable the headlines are still ranked (by recency) and shown
with an excerpt as the description.

Per-company fetches are cached for NEWS_TTL_HOURS (news is not tied to a
reporting period); ranking happens at read time, so it is free to retune.
"""
import asyncio
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from competitor_analysis import config, paths
from competitor_analysis.extraction import gemini
from competitor_analysis.storage import s3

CACHE_DIR = paths.CACHE_DIR / "news"
NEWS_TTL_HOURS = float(os.getenv("NEWS_TTL_HOURS", "6"))
NEWS_TOP_N = int(os.getenv("NEWS_TOP_N", "8"))
NEWS_MAX_PER_COMPANY = int(os.getenv("NEWS_MAX_PER_COMPANY", "3"))
LOOKBACK_DAYS = 30
SEARCH_TIMEOUT_SECONDS = 60
MAX_RESULTS = 10
MAX_ITEMS_PER_COMPANY = 8  # cached per company; the final list is cut to NEWS_TOP_N
EXCERPT_CHARS = 600
DESCRIPTION_CHARS = 220  # fallback description when there is no Gemini summary
DEFAULT_IMPORTANCE = 3  # unscored articles (no Gemini, or cached before scoring existed)
TAGS = ["Product", "Regulatory", "Financial", "Partnership", "Leadership", "Claims/Customer", "Other"]

# The competitors we track (Niva Bupa is "us" and deliberately absent). Friendly
# names are used in search queries; the legal name comes from source_links.json.
_SEARCH_NAMES = {
    "STAR": "Star Health", "CARE": "Care Health", "CIGNA": "ManipalCigna",
    "ABHI": "Aditya Birla Health", "NARAYANA": "Narayana Health Insurance", "GALAXY": "Galaxy Health Insurance",
}
COMPANY_ORDER = list(_SEARCH_NAMES)

_client = None
_limiter = None


class NewsUnavailable(Exception):
    """PARALLEL_API_KEY is not configured."""


def _search_client():
    global _client
    if _client is None:
        key = os.getenv("PARALLEL_API_KEY")
        if not key:
            raise NewsUnavailable("PARALLEL_API_KEY is not set")
        from parallel import AsyncParallel  # lazy: importing this module must not need the key
        _client = AsyncParallel(api_key=key)
    return _client


def _legal_names():
    """short key -> {legal name, disclosure domain} from source_links.json."""
    try:
        sources = json.loads(paths.SOURCE_LINKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for source_key, entry in sources.items():
        short = config.short_key_for_source(source_key)
        if short:
            link = (entry or {}).get("quarterly_disclosure_page_link") or ""
            out[short] = {"legal": (entry or {}).get("name") or source_key, "domain": urlparse(link).netloc}
    return out


def _is_own_filing(url, domain):
    p = urlparse(url)
    return p.path.lower().endswith(".pdf") or (bool(domain) and p.netloc == domain and "disclosure" in p.path.lower())


async def _search(company):
    info = _legal_names().get(company, {})
    name = _SEARCH_NAMES[company]
    legal = info.get("legal") or name
    after = date.today() - timedelta(days=LOOKBACK_DAYS)
    result = await asyncio.wait_for(
        _search_client().search(
            objective=(f"Recent news (last {LOOKBACK_DAYS} days) about {legal}, an Indian standalone health insurer: "
                       "products, IRDAI/regulatory, financial results, funding/IPO, leadership, partnerships, "
                       "claims and complaints."),
            search_queries=[f"{name} health insurance news", legal, f"{name} IRDAI"],
            mode="basic",
            advanced_settings={"max_results": MAX_RESULTS, "location": "IN",
                               "source_policy": {"after_date": after.isoformat()}},
        ),
        timeout=SEARCH_TIMEOUT_SECONDS,
    )
    seen, items = set(), []
    for r in result.results:
        if not r.url or r.url in seen or _is_own_filing(r.url, info.get("domain")):
            continue
        seen.add(r.url)
        excerpt = (r.excerpts[0] if r.excerpts else "")[:EXCERPT_CHARS]
        items.append({"url": r.url, "title": (r.title or r.url).strip(), "publish_date": r.publish_date,
                      "excerpt": excerpt, "source": urlparse(r.url).netloc.removeprefix("www.")})
    return items


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"articles": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "relevant": {"type": "boolean"},
            "summary": {"type": "string"},
            "tag": {"type": "string", "enum": TAGS},
            "importance": {"type": "integer"},
            "duplicate_of": {"type": "string", "nullable": True},
            "publish_date": {"type": "string", "nullable": True},
        },
        "required": ["url", "relevant", "summary", "tag", "importance"],
    }}},
    "required": ["articles"],
}

_PROMPT = """You are screening web search results for news about {legal} ({name}), an Indian standalone health insurer.
The readers are the strategy team at a competing insurer. For every article below return:
- relevant: true only if it is a specific news story about this insurer. False for generic listicles, articles about
  a different company, policy-document or product-brochure pages, and archive / tag / company-profile / stock-quote
  / "all news" listing pages.
- duplicate_of: if the article reports the same event as an EARLIER article in the list (e.g. two outlets covering
  the same results or launch), the url of that earlier article; otherwise null.
- summary: what happened, in at most 35 words, stating the news itself (no "this article").
- tag: one of {tags}.
- importance: integer 1-5 for how much a competitor's strategy team should care. Be discriminating - across a batch
  most articles should be 2 or 3 and at most one or two a 5. 5 = regulatory action or penalty, funding or IPO,
  leadership change, a landmark product launch or partnership; 4 = quarterly results or other material financial
  news, a significant launch; 3 = notable but incremental (smaller launches, awards, campaigns); 2 = minor or
  routine; 1 = PR filler, advice columns, passing mentions.
- publish_date: YYYY-MM-DD if the title or excerpt shows one, else null.
Return every url exactly as given.

Articles:
{articles}"""


async def _summarise(company, items):
    """Adds summary/tag/importance to each item and drops irrelevant ones. Raises on any Gemini failure."""
    global _limiter
    if _limiter is None:
        _limiter = gemini.RateLimiter(gemini.GEMINI_RPM)
    info = _legal_names().get(company, {})
    prompt = _PROMPT.format(
        legal=info.get("legal") or _SEARCH_NAMES[company], name=_SEARCH_NAMES[company], tags=TAGS,
        articles=json.dumps([{k: i[k] for k in ("url", "title", "publish_date", "excerpt")} for i in items],
                            ensure_ascii=False))

    def call():
        resp = gemini.client().models.generate_content(
            model=gemini.MODEL, contents=prompt,
            config=gemini.types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=_SUMMARY_SCHEMA, temperature=0))
        return json.loads(resp.text)["articles"]

    await _limiter.acquire()
    verdicts = {a["url"]: a for a in await asyncio.to_thread(call)}
    out = []
    for i in items:
        v = verdicts.get(i["url"])
        if v is None or not v.get("relevant") or v.get("duplicate_of"):
            continue
        out.append(i | {"summary": v["summary"].strip(), "tag": v["tag"],
                        "importance": min(5, max(1, int(v["importance"]))),
                        "publish_date": i["publish_date"] or v.get("publish_date")})
    return out


def _cache_path(company):
    return CACHE_DIR / f"{company}.json"


def _fresh(entry):
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, ValueError):
        return False
    return age < timedelta(hours=NEWS_TTL_HOURS)


async def _fetch_company(company, refresh):
    path = _cache_path(company)
    if not refresh and path.exists():
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if _fresh(entry):
                return entry | {"cached": True}
        except (OSError, ValueError):
            pass

    items = await _search(company)
    summarised = False
    if items and os.getenv("GEMINI_API_KEY"):
        try:
            items, summarised = await _summarise(company, items), True
        except Exception:  # noqa: BLE001 - quota/outage: headlines are still worth showing
            summarised = False
    # The search's date filter is not strict; drop anything dated outside the window.
    oldest = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    items = [i for i in items if not i.get("publish_date") or i["publish_date"][:10] >= oldest]
    items = sorted(items, key=lambda i: i.get("publish_date") or "", reverse=True)[:MAX_ITEMS_PER_COMPANY]
    entry = {"company": company, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "items": items, "summarised": summarised}
    os.makedirs(CACHE_DIR, exist_ok=True)
    path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    s3.upload_file(path)
    return entry | {"cached": False}


def _age_days(publish_date, today):
    try:
        return max(0, (today - date.fromisoformat((publish_date or "")[:10])).days)
    except ValueError:
        return None


def _score(item, today):
    """Importance (1-5) plus up to 1 point of recency, fading to 0 over the lookback window."""
    age = _age_days(item.get("publish_date"), today)
    recency = 0.0 if age is None else max(0.0, 1 - age / LOOKBACK_DAYS)
    return item.get("importance", DEFAULT_IMPORTANCE) + recency


def rank(entries, limit=None, per_company=None, today=None):
    """Pools every company's articles, merges the same URL across companies, and returns the top `limit`
    with at most `per_company` per insurer (a merged article counts against each insurer it names)."""
    limit = NEWS_TOP_N if limit is None else limit
    per_company = NEWS_MAX_PER_COMPANY if per_company is None else per_company
    today = today or date.today()

    pooled = {}
    for entry in entries:
        for item in entry.get("items", []):
            merged = pooled.setdefault(item["url"], item | {"companies": []})
            if entry["company"] not in merged["companies"]:
                merged["companies"].append(entry["company"])

    ordered = sorted(pooled.values(), key=lambda i: (_score(i, today), i.get("publish_date") or ""), reverse=True)
    taken, counts = [], {}
    for item in ordered:
        if len(taken) >= limit:
            break
        if any(counts.get(c, 0) >= per_company for c in item["companies"]):
            continue
        for c in item["companies"]:
            counts[c] = counts.get(c, 0) + 1
        taken.append(item)

    return [{"title": i["title"], "url": i["url"], "source": i["source"], "publish_date": i.get("publish_date"),
             "companies": i["companies"], "tag": i.get("tag"), "importance": i.get("importance"),
             "description": i.get("summary") or i.get("excerpt", "")[:DESCRIPTION_CHARS].strip()}
            for i in taken]


async def get_news(company=None, refresh=False, limit=None):
    """The top ranked competitor news. `company` (debugging) restricts the pool to one insurer.
    One company failing never fails the rest; it is reported in `errors`."""
    if company is not None and company not in COMPANY_ORDER:
        raise ValueError(f"Unknown company '{company}'. Use one of: {', '.join(COMPANY_ORDER)}")
    keys = [company] if company else list(COMPANY_ORDER)
    results = await asyncio.gather(*(_fetch_company(k, refresh) for k in keys), return_exceptions=True)
    entries, errors = [], []
    for k, r in zip(keys, results):
        if isinstance(r, NewsUnavailable):
            raise r
        if isinstance(r, Exception):
            errors.append({"company": k, "error": str(r)[:200] or type(r).__name__})
        else:
            entries.append(r)
    return {"items": rank(entries, limit), "errors": errors, "lookback_days": LOOKBACK_DAYS,
            "summarised": bool(entries) and all(e.get("summarised") for e in entries if e["items"]),
            "cached": bool(entries) and all(e.get("cached") for e in entries),
            "fetched_at": min((e["fetched_at"] for e in entries), default=None)}
