"""
Recent news per competitor, for the dashboard's News tab.

Phase 1 of a fetch is a Parallel Search API call per insurer (last 30 days);
phase 2 is one Gemini call per insurer that drops off-topic hits and adds a
one-line summary and a category tag. If Gemini is unavailable the headlines
are still returned, flagged `summarised: False`.

Results are cached per company (news is not tied to a reporting period) for
NEWS_TTL_HOURS, so opening the tab doesn't cost a search each time.
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
LOOKBACK_DAYS = 30
SEARCH_TIMEOUT_SECONDS = 60
MAX_RESULTS = 10
MAX_ITEMS = 8
EXCERPT_CHARS = 600
TAGS = ["Product", "Regulatory", "Financial", "Partnership", "Leadership", "Claims/Customer", "Other"]

# Friendly names for search queries; the legal name comes from source_links.json.
_SEARCH_NAMES = {
    "NBHI": "Niva Bupa", "STAR": "Star Health", "CARE": "Care Health", "CIGNA": "ManipalCigna",
    "ABHI": "Aditya Birla Health", "NARAYANA": "Narayana Health Insurance", "GALAXY": "Galaxy Health Insurance",
}

# Same order as reporting.data.COMPANY_ORDER (not imported: that module needs a reporting period set at import).
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
            "publish_date": {"type": "string", "nullable": True},
        },
        "required": ["url", "relevant", "summary", "tag"],
    }}},
    "required": ["articles"],
}

_PROMPT = """You are screening web search results for news about {legal} ({name}), an Indian standalone health insurer.
For every article below return: relevant (true only if it is genuinely about this insurer - not a generic listicle,
a different company, or a policy-document page), a summary of at most 25 words stating the news itself (no
"this article"), a tag from {tags}, and publish_date as YYYY-MM-DD if the title or excerpt shows one, else null.
Return every url exactly as given.

Articles:
{articles}"""


async def _summarise(company, items):
    """Adds summary/tag to each item and drops irrelevant ones. Raises on any Gemini failure."""
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
        if v is None or not v.get("relevant"):
            continue
        out.append(i | {"summary": v["summary"].strip(), "tag": v["tag"],
                        "publish_date": i["publish_date"] or v.get("publish_date")})
    return out


def _sort_key(item):
    return item.get("publish_date") or ""


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
    items = sorted(items, key=_sort_key, reverse=True)[:MAX_ITEMS]
    entry = {"company": company, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "items": items, "summarised": summarised}
    os.makedirs(CACHE_DIR, exist_ok=True)
    path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    s3.upload_file(path)
    return entry | {"cached": False}


async def get_news(company=None, refresh=False):
    """News for one company key, or all seven. One company failing never fails the rest."""
    if company is not None and company not in COMPANY_ORDER:
        raise ValueError(f"Unknown company '{company}'. Use one of: {', '.join(COMPANY_ORDER)}")
    keys = [company] if company else list(COMPANY_ORDER)
    results = await asyncio.gather(*(_fetch_company(k, refresh) for k in keys), return_exceptions=True)
    companies = []
    for k, r in zip(keys, results):
        if isinstance(r, NewsUnavailable):
            raise r
        if isinstance(r, Exception):
            r = {"company": k, "items": [], "summarised": False, "cached": False,
                 "error": str(r)[:200] or type(r).__name__}
        companies.append(r)
    return {"companies": companies, "lookback_days": LOOKBACK_DAYS,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
