import os
import json
import re
import asyncio
import ssl
from collections import Counter
from pathlib import Path
from html import unescape
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit

import httpx
import aiofiles
import openpyxl
from pypdf import PdfReader
from parallel import AsyncParallel  # Parallel AI Python SDK
from dotenv import load_dotenv

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis import logging_setup

log = logging_setup.get_logger(__name__)

load_dotenv()

# 1. Setup paths & client
# Paths resolve through competitor_analysis.paths, not this file's location.
LINKS_FILE = paths.SOURCE_LINKS_FILE
DOWNLOAD_DIR = paths.DOWNLOADS_DIR

client = AsyncParallel(api_key=os.getenv("PARALLEL_API_KEY"))

# "core" gives more reasoning depth than "base" for navigating dropdown/accordion
# filters before the real file link resolves in the DOM.
# https://docs.parallel.ai/task-api/guides/choose-a-processor
PROCESSOR = "core"

# Per-processor wall-clock budget for task_run.result(). A single global
# 30-minute ceiling once let one hung "core" run block the next processor for
# half an hour, so budgets are bounded - but they must also be long enough for a
# browsing agent to actually finish. At 60s a normal "core"/"pro" run was cut off
# while still working (and still billing server-side), escalated, and then
# failed again, leaving the Search branch to win. The agent is now a fallback
# behind cached URLs and the listing-page scrape, so paying for patience only on
# the rare company that reaches it is the right trade.
PROCESSOR_TIMEOUTS = {"core": 180, "pro": 300, "ultra": 600}
DEFAULT_PROCESSOR_TIMEOUT = 180

# Escalation ladder, cheapest first. "ultra" is deliberately NOT here: it costs
# materially more than core/pro and, being the third sequential attempt, it also
# inflated worst-case latency exactly when a portal was already failing. Any
# company can opt back into a different ladder via a "processors" list in
# source_links.json (e.g. ["core", "pro", "ultra"]) without a code change.
PROCESSOR_LADDER = ["core", "pro"]

# Search API budget - it's the cheap safety net, so it gets a short leash too.
SEARCH_TIMEOUT_SECONDS = 60

# Run the Search API concurrently with the FIRST processor rather than only
# after every processor has failed. Whichever produces a downloadable file first
# wins and the loser is cancelled, so a portal the agent can't crawl no longer
# pays the full ladder before Search is even tried.
RACE_SEARCH_WITH_FIRST_PROCESSOR = True

# Resolved-URL memory. Disclosure URLs are overwhelmingly templated by period,
# so a URL that worked once is turned into a {month}/{year}-parameterised
# pattern and tried directly next quarter - which skips the agent entirely for
# most companies. This is a cache, never a source of truth: a rendered pattern
# still has to pass download_file's magic-bytes and validate_document checks.
URL_PATTERNS_FILE = paths.URL_PATTERNS_FILE

# How each company's file was located this run, for the end-of-run summary -
# this is what tells you which processors actually earn their place in the
# ladder and which never win.
ATTEMPT_LOG: dict[str, dict] = {}

# The candidate URL that try_candidates_concurrently last saved per company, so
# a route that only receives the saved path can still cache the URL's template.
WINNING_URL: dict[str, str] = {}

# Browser-like headers so Cloudflare/AWS WAF on the insurer domains doesn't 403/406
# the actual file download (see "Web Application Firewall Protections" in the brief).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Magic bytes to confirm we actually got the binary file and not an HTML WAF
# challenge page or a login/error page served with a 200 status.
FILE_MAGIC = {
    "pdf": b"%PDF",
    "xlsx": b"PK",
}

# Per-response cap. The Search API fallback is deliberately NOT
# domain-restricted (disclosure PDFs are often off-domain, on a CDN), so a
# candidate URL is not guaranteed to be a small PDF - it can be an arbitrary
# webpage. Without a cap, a wrong candidate gets fully buffered into memory
# before validate_document ever gets a chance to reject it. Mirrors
# MAX_UPLOAD_BYTES in api/main.py - same problem, download side instead of
# upload side.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # a regulatory filing runs a few MB
DOWNLOAD_CHUNK_BYTES = 256 * 1024

# Global concurrency cap across the WHOLE Phase 1 run, not per company.
# Companies already run fully concurrently (main()'s asyncio.gather), and
# each one can itself race up to 8 Search candidates or several cached-URL
# variants concurrently (try_candidates_concurrently) - unbounded, that is
# up to ~8 companies x 8 candidates = 64 full responses potentially
# buffered in memory at once. That combination is what actually OOM'd a
# 512MB Render instance during retrieval - a distinct gap from Phase 2
# extraction, which already has its own worker/memory tuning
# (PDF_PARSE_MAX_WORKERS, MEMORY_BUDGET_MB).
MAX_CONCURRENT_DOWNLOADS = 4
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

def _ssl_context() -> ssl.SSLContext | bool:
    """TLS trust for downloads, with verification ON.

    Some insurer hosts (Aditya Birla's adityabirlacapital.com and
    adityabirlahealthinsurance.com) serve a chain that the operating system
    accepts but httpx's bundled certifi store rejects ("unable to get local
    issuer certificate"), which made retrieval fail for that company. Trusting
    the OS store fixes it without weakening verification. Falls back to the
    certifi default if truststore is unavailable."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as e:  # ImportError, or a platform truststore can't support
        log.warning("truststore unavailable (%s); using the bundled CA store.", e)
        return True


MONTH_ABBREV = {
    "january": "jan", "february": "feb", "march": "mar", "april": "apr",
    "may": "may", "june": "jun", "july": "jul", "august": "aug",
    "september": "sep", "october": "oct", "november": "nov", "december": "dec",
}


def load_sources() -> dict:
    with open(LINKS_FILE, "r") as f:
        return json.load(f)


def load_url_patterns() -> dict:
    if not URL_PATTERNS_FILE.exists():
        return {}
    try:
        with open(URL_PATTERNS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Ignoring unreadable %s: %s", URL_PATTERNS_FILE.name, e)
        return {}


def save_url_pattern(company_key: str, url: str, cal_data: dict, quarter: str):
    """Remember a working URL as a period-parameterised template."""
    template = templatize_url(url, cal_data, quarter)
    if template is None:
        return  # nothing period-specific in it, so it can't be re-rendered
    patterns = load_url_patterns()
    if patterns.get(company_key) == template:
        return
    patterns[company_key] = template
    try:
        paths.ensure(URL_PATTERNS_FILE.parent)
        with open(URL_PATTERNS_FILE, "w") as f:
            json.dump(patterns, f, indent=2, sort_keys=True)
        log.info("[%s] Cached URL pattern -> %s", company_key, template)
    except OSError as e:
        log.warning("[%s] Could not write URL pattern cache: %s", company_key, e)


# Query-string markers of a pre-signed, single-file, time-limited URL (Azure
# SAS, S3 presigned, generic tokens). These can never be re-rendered for
# another period: the payload is one specific blob and the signature covers the
# very parameters a template would rewrite. Caching them produced a template
# with placeholders substituted into a SAS expiry timestamp, which then
# rendered a URL whose signature no longer matched.
_SIGNED_URL_MARKERS = ("sig=", "signature=", "x-amz-", "token=", "se=&", "&se=", "?se=")


def _is_signed_url(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in _SIGNED_URL_MARKERS)


def _period_tokens(cal_data: dict, quarter: str) -> list[tuple[str, str]]:
    """(literal, placeholder) pairs for the period spellings that can appear in
    a disclosure URL, longest-first so 'december' is replaced before 'dec' and
    the 4-digit year before any 2-digit form.

    Deliberately excludes a bare 2-digit year: "25" matches inside unrelated
    numbers (document ids, timestamps, paths), and substituting a placeholder
    there silently rewrites the URL to point at a different file. Only
    delimited 2-digit forms are safe, and those are covered by the FY labels
    below."""
    month = cal_data["month"]
    year = cal_data["year"]
    abbrev = MONTH_ABBREV[month.lower()]
    yy, next_yy = str(year)[-2:], str(year + 1)[-2:]
    return [
        (f"{yy}-{next_yy}", "{fy_short}"),
        (f"{year}-{next_yy}", "{fy_label}"),
        (month, "{month}"),
        (abbrev, "{mon}"),
        (str(year), "{year}"),
        (quarter, "{quarter}"),
    ]


_OPAQUE_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def templatize_url(url: str, cal_data: dict, quarter: str) -> str | None:
    """Turn a working URL into a period-parameterised template, or None if it
    cannot safely be one.

    Only the path is templatised. The query string is left untouched because
    that is where signatures, tokens and API versions live - rewriting a date
    inside those either invalidates the signature or points at a different
    object. Pre-signed URLs are rejected outright for the same reason.
    """
    if _is_signed_url(url):
        return None
    parts = urlsplit(url)
    path = parts.path
    # A document-store id (e.g. a Liferay UUID) names one specific upload, so a
    # URL carrying one can never be re-rendered for another period.
    if _OPAQUE_ID_RE.search(path):
        return None
    replaced = False
    month_replaced = False
    for literal, placeholder in _period_tokens(cal_data, quarter):
        if not literal:
            continue
        pattern = re.compile(re.escape(literal), re.IGNORECASE)
        if pattern.search(path):
            path = pattern.sub(placeholder, path)
            replaced = True
            month_replaced = month_replaced or placeholder in ("{month}", "{mon}")
    # "Disclosure-December-25.pdf": a 2-digit year is only safe to templatise when
    # it is delimited on both sides AND sits in a path that also named the month,
    # otherwise "-25" could be any number. Left hardcoded it makes the cached
    # URL unusable next year.
    if month_replaced:
        yy = str(cal_data["year"])[-2:]
        yy_re = re.compile(rf"(?<=[-_'])({yy})(?=[._/\-]|$)")
        if yy_re.search(path):
            path = yy_re.sub("{yy}", path)
    if not replaced:
        return None
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def render_url_template(template: str, cal_data: dict, quarter: str) -> list[str]:
    """Render a cached template for a period. Returns several casing variants,
    since insurers are inconsistent about capitalising month names in paths."""
    month = cal_data["month"]
    year = cal_data["year"]
    abbrev = MONTH_ABBREV[month.lower()]
    variants = []
    for month_form, mon_form, q_form in (
        (month, abbrev, quarter),
        (month.lower(), abbrev.lower(), quarter.lower()),
        (month.capitalize(), abbrev.capitalize(), quarter.upper()),
        (month.upper(), abbrev.upper(), quarter.upper()),
    ):
        rendered = (template
                    .replace("{month}", month_form)
                    .replace("{mon}", mon_form)
                    .replace("{quarter}", q_form)
                    .replace("{year}", str(year))
                    .replace("{yy}", str(year)[-2:])
                    .replace("{fy_short}", f"{str(year)[-2:]}-{str(year + 1)[-2:]}")
                    .replace("{fy_label}", f"{year}-{str(year + 1)[-2:]}"))
        if rendered not in variants:
            variants.append(rendered)
    return variants


def ladder_for(data: dict) -> list[str]:
    """Per-company processor ladder, falling back to the shared default. Lets a
    single stubborn portal keep an expensive tier without every company paying
    for it."""
    ladder = data.get("processors")
    if isinstance(ladder, list) and ladder:
        return ladder
    return PROCESSOR_LADDER


def ensure_download_dir(fy: str, quarter: str) -> Path:
    target_dir = DOWNLOAD_DIR / fy / quarter
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


# 2. Financial Year Calendar Mapping
def get_calendar_mapping(fy: str, quarter: str) -> dict:
    """Maps FY26 Q3 to calendar year 2025, Month December"""
    return cfg.calendar_mapping(fy, quarter.upper())


# 3. Parallel Task API — structured output schema
# https://docs.parallel.ai/core-concepts/task-spec
def build_output_schema(file_type: str) -> dict:
    ext = file_type.lower()
    return {
        "type": "json",
        "json_schema": {
            "type": "object",
            "properties": {
                "found": {
                    "type": "boolean",
                    "description": (
                        "True only if a direct, working download link to the matching "
                        "document was located on the site."
                    ),
                },
                "download_url": {
                    "type": "string",
                    "description": (
                        f"The exact, direct download URL for the {file_type} file "
                        f"(should resolve to a binary .{ext} download, not an HTML "
                        f"landing page). Empty string if not found."
                    ),
                },
                "document_title": {
                    "type": "string",
                    "description": "The title, link text, or filename of the matched document as shown on the page.",
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "If not found: what you actually saw — e.g. which tabs/filters/categories "
                        "exist, whether a category showed 'No records found' or an API error, or "
                        "whether the period simply isn't published yet. Empty string if found."
                    ),
                },
            },
            "required": ["found", "download_url", "document_title", "notes"],
        },
    }


def build_objective(company_key: str, url: str, cal_data: dict, fy: str,
                    company: dict | None = None) -> tuple[str, str]:
    is_gic = company_key == "GIC"
    file_type = "XLSX" if is_gic else "PDF"

    anti_hallucination = (
        " Only return a URL that you can actually see rendered as a link or button in the "
        "page's own content — never invent or pattern-match a filename by copying the naming "
        "convention of a different month/quarter you did find, since that file may not exist. "
        "If the period requested isn't in the default/most-recent listing, look for an "
        "archive, 'previous years', or older-reports section before giving up."
    )

    if is_gic:
        objective = (
            f"Go to {url}, the General Insurance Council's segment-wise report page. "
            f"It lists monthly cumulative segment-wise premium data tables. Find the "
            f"row or link for the month of {cal_data['month']} {cal_data['year']} "
            f"(the cumulative figure for the quarter ending that month) and return the "
            f"direct Excel (.xlsx) download URL for that month." + anti_hallucination
        )
    else:
        # From the FY itself, not the quarter's calendar year - see cfg.fy_label.
        label = cfg.fy_label(fy)
        label_long = cfg.fy_label_long(fy)
        legal = (company or {}).get("name") or company_key
        reg = (company or {}).get("irdai_reg_no")
        identity = legal + (f" (IRDAI registration no. {reg})" if reg else "")
        objective = (
            f"Go to {url}, the public disclosures page for {identity}. Only a document "
            f"published by this exact insurer counts - not a peer's or its parent's. Find the "
            f"Quarterly Public Disclosure document for Financial Year {label} "
            f"(also written {label_long}), the "
            f"quarter ending {cal_data['month']} {cal_data['year']}. If the page has a "
            f"financial-year dropdown, filter tab, accordion section, or a document-category "
            f"filter (e.g. a 'Download Center' with categories like 'Public Disclosure' or "
            f"'Financial Results'), select or expand the one for FY {label} first. Ignore unrelated documents such "
            f"as stewardship/voting disclosures, investor presentations, media "
            f"clippings, or annual reports — only the quarterly public disclosure PDF "
            f"for this exact quarter counts. Return the direct PDF download URL." + anti_hallucination
        )
    return objective, file_type


async def resolve_download_url(
    company_key: str, url: str, cal_data: dict, processor: str, fy: str,
    feedback: str | None = None, company: dict | None = None
) -> tuple[dict | None, str]:
    objective, file_type = build_objective(company_key, url, cal_data, fy, company)
    if feedback:
        objective += " " + feedback
    domain = urlparse(url).netloc
    domains = [domain] + [d for d in (company or {}).get("allowed_domains", []) if d != domain]

    task_run = await client.task_run.create(
        input=objective,
        processor=processor,
        task_spec={"output_schema": build_output_schema(file_type)},
        # Keep the agent on the insurer's own domain instead of wandering the web.
        # https://docs.parallel.ai (SourcePolicy.include_domains)
        source_policy={"include_domains": domains},
    )
    budget = PROCESSOR_TIMEOUTS.get(processor, DEFAULT_PROCESSOR_TIMEOUT)
    log.info("[%s] Task submitted (run_id=%s, processor=%s, budget=%ds), waiting for result...",
              company_key, task_run.run_id, processor, budget)

    # Bound the wait twice over: the SDK's own api_timeout, plus an outer
    # wait_for so a call that ignores or exceeds it still can't stall the
    # ladder. Abandoning the wait leaves the run going server-side, which is
    # fine - we simply stop paying wall-clock for it.
    result = await asyncio.wait_for(
        client.task_run.result(task_run.run_id, api_timeout=budget),
        timeout=budget + 15,
    )
    content = result.output.content

    if not content.get("found") or not content.get("download_url"):
        log.warning("[%s] Agent did not find a matching %s. Notes: %s",
                    company_key, file_type, content.get("notes"))
        return None, file_type

    log.info("[%s] Found: %r -> %s", company_key, content.get("document_title"), content["download_url"])
    return content, file_type


# How many leading PDF pages to read when validating. The IRDAI form
# schedules start after a cover/index page or two, so page 1 alone is not
# enough to confirm we actually got a disclosure bundle.
VALIDATION_PAGES = 6

# Every genuine IRDAI quarterly public disclosure is a bundle of numbered
# "FORM NL-n" schedules. Requiring at least one is what separates the real
# filing from the things that otherwise sail through a period-only check:
# the insurer's LISTED PARENT's stock-exchange results, a BSE covering
# letter, a different disclosure series ("Health Services Rendered"), or - as
# actually happened - a completely unrelated PDF returned by a web search.
_FORM_NL_RE = re.compile(r"FORM\s*NL-\s*\d+", re.I)

_MONTH_NUMBER = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def extract_lead_text(path: Path, ext: str, pages: int = VALIDATION_PAGES) -> str:
    """Text from the first `pages` PDF pages, or the XLSX title row (GIC's
    title row states the period covered)."""
    if ext == "pdf":
        reader = PdfReader(str(path))
        out = []
        for page in reader.pages[:pages]:
            out.append(page.extract_text() or "")
        return "\n".join(out)
    if ext == "xlsx":
        # Open the handle ourselves: openpyxl refuses a path whose extension isn't
        # .xlsx, and candidates are saved under temp names like ".GIC.xlsx.cand0".
        with open(path, "rb") as fh:
            wb = openpyxl.load_workbook(fh, data_only=True, read_only=True)
            first_row = next(wb.active.iter_rows(min_row=1, max_row=1, values_only=True), ())
        return " ".join(str(c) for c in first_row if c)
    return ""


def _period_regexes(month: str, year: str):
    mon = MONTH_ABBREV[month]
    yy = year[-2:]
    mnum = _MONTH_NUMBER[month]
    # "June 2026", "30th JUNE , 2026", "period ended June 30, 2026". A bare
    # 2-digit year is NOT allowed here: "December 25, 2024" would otherwise read
    # as December 2025.
    named = re.compile(
        rf"\b({month}|{mon})\b[^0-9A-Za-z]{{0,4}}(\d{{1,2}}[^0-9A-Za-z]{{0,4}})?{year}\b", re.I)
    # "Jun-26", "Dec'25" - the 2-digit year must follow a delimiter directly.
    short = re.compile(rf"\b({month}|{mon})\s?[-'’/]\s?{yy}\b", re.I)
    # "30/06/2026", "30-6-26"
    numeric = re.compile(rf"\b\d{{1,2}}[/-]0?{mnum}[/-]({year}|{yy})\b")
    return named, short, numeric


def _mentions_period(text: str, cal_data: dict) -> bool:
    """Whether `text` states the target month AND year *together*.

    Requiring adjacency matters: a filing for a different quarter will often
    contain the target year somewhere (a maturity date, a signing date) and
    the target month somewhere else, so testing the two independently passes
    documents that cover an entirely different period."""
    month = cal_data["month"].lower()
    year = str(cal_data["year"])
    return any(rx.search(text) for rx in _period_regexes(month, year))


_MONTH_ALT = "|".join(sorted(
    list(_MONTH_NUMBER) + list(MONTH_ABBREV.values()) + ["sept"], key=len, reverse=True))

# "for the quarter ended 30th June, 2026", "Up to the period ended Dec 31, 2025",
# "FOR THE YEAR ENDED MARCH 31, 2025" - the statement a filing's cover/header
# makes about the period it covers, as opposed to a comparative column or a
# date mentioned in passing.
_STATED_PERIOD_RE = re.compile(
    rf"(?:period|quarter|year|months?)\s+(?:ended|ending|upto|up\s+to)\s*[:\-]?\s*(?:the\s+)?"
    rf"(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?[\s\-./,]*(?P<m1>{_MONTH_ALT})[\s\-./,']*(?P<y1>\d{{4}})"
    rf"|(?P<m2>{_MONTH_ALT})\.?[\s\-./,]*(?:\d{{1,2}}(?:st|nd|rd|th)?[\s\-./,]*)?(?P<y2>\d{{4}}))",
    re.I)


def _month_number(token: str) -> int:
    return _MONTH_NUMBER[next(m for m in _MONTH_NUMBER if m.startswith(token.lower()[:3]))]


def _stated_period(text: str) -> tuple[int, int] | None:
    """(month, year) of the FIRST 'period/quarter/year ended <date>' statement."""
    m = _STATED_PERIOD_RE.search(text)
    if not m:
        return None
    month = m.group("m1") or m.group("m2")
    year = m.group("y1") or m.group("y2")
    return _month_number(month), int(year)


def _period_is_stated_first(text: str, cal_data: dict) -> bool:
    """The first period the document says it covers must be the target.

    Filings carry comparatives and cite other dates, so merely mentioning the
    target quarter-end somewhere (what `_mentions_period` tests) also passes a
    different quarter's filing. Counting mentions does not separate them either
    - current and prior-year columns appear about equally often - so use the
    header statement. A document with no such statement is not rejected here;
    `_mentions_period` already vouched for it."""
    stated = _stated_period(text)
    if stated is None:
        return True
    return stated == (_MONTH_NUMBER[cal_data["month"].lower()], int(cal_data["year"]))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("&", " and "))


def _company_entry(company_key: str) -> dict | None:
    try:
        return load_sources().get(company_key)
    except (OSError, json.JSONDecodeError):
        return None


def _mentions_company(text: str, company: dict) -> bool:
    """Whether `text` names this insurer (any alias) or carries its IRDAI
    registration number."""
    norm = _norm(text)
    for alias in company.get("aliases", []):
        if _norm(alias) in norm:
            return True
    reg = company.get("irdai_reg_no")
    if reg and re.search(
            rf"reg(?:istration|n)?\.?\s*(?:no\.?|number)?\s*[:\-]?\s*{re.escape(str(reg))}\b",
            norm):
        return True
    return False


def validate_document(path: Path, ext: str, cal_data: dict, company_key: str) -> bool:
    """Confirm the downloaded file is the right COMPANY's right DOCUMENT for the
    right PERIOD, not merely a well-formed PDF/XLSX that happens to mention the
    date.

    Independent checks, because each alone lets real mistakes through:

      * period  - catches an insurer reusing or mislabelling a stale file
                  under the current quarter's link (seen with Galaxy Health,
                  whose "Q3"-named asset contained Sep-30 content). The first
                  "period ended ..." statement must also be the target, so a
                  filing for another quarter that merely cites the target
                  date as a comparative fails.
      * form    - catches a right-period file that isn't the disclosure at
                  all. A period-only check accepted Aditya Birla *Capital*'s
                  quarterly results, a Star Health covering letter to BSE,
                  and an unrelated third-party PDF from web search - all of
                  which mention the quarter-end date somewhere.
      * company - every insurer's disclosure has the same FORM NL layout, so a
                  Care Health filing found by Search passes both checks above
                  when asked for Star Health. Needs "aliases"/"irdai_reg_no" in
                  source_links.json; skipped for a company without them.
    """
    try:
        text = extract_lead_text(path, ext)
    except Exception as e:
        # Unreadable is unverifiable: accepting it is how a wrong file used to
        # slip through, so refuse it and let the next candidate/attempt run.
        log.warning("[%s] Rejected %s: could not read it to verify (%s).",
                    company_key, path.name, e)
        return False

    if not _mentions_period(text, cal_data):
        log.warning("[%s] Rejected %s: does not state the period %s %s.",
                    company_key, path.name, cal_data["month"], cal_data["year"])
        return False

    if not _period_is_stated_first(text, cal_data):
        log.warning("[%s] Rejected %s: mentions %s %s but its header covers a different "
                    "period (likely another quarter's filing).",
                    company_key, path.name, cal_data["month"], cal_data["year"])
        return False

    # The GIC workbook is a statistics table, not a disclosure bundle, so the
    # form and company checks don't apply to it - its title row already names
    # the period.
    if ext == "xlsx":
        return True

    if not _FORM_NL_RE.search(text):
        log.warning("[%s] Rejected %s: no 'FORM NL-n' schedule in the first %d pages, "
                    "so this is not an IRDAI public disclosure (wrong document, right date).",
                    company_key, path.name, VALIDATION_PAGES)
        return False

    company = _company_entry(company_key)
    if company and (company.get("aliases") or company.get("irdai_reg_no")):
        if not _mentions_company(text, company):
            log.warning("[%s] Rejected %s: does not name this insurer (no alias or IRDAI "
                        "registration number found) - likely another company's filing.",
                        company_key, path.name)
            return False
    return True


async def _fetch_bounded(http_client: httpx.AsyncClient, url: str, magic: bytes,
                         file_type: str, company_key: str, attempt: int) -> bytes | None:
    """GET `url` streamed in chunks under the global download semaphore,
    bailing out - without buffering the rest of the response - the moment
    it's clearly too big or (once enough bytes have arrived) clearly isn't
    the right file type. Returns the full body on success, None on any
    rejection (already logged)."""
    async with _download_semaphore:
        async with http_client.stream("GET", url) as resp:
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                buf.extend(chunk)
                if len(buf) > MAX_DOWNLOAD_BYTES:
                    log.warning(
                        "[%s] Candidate exceeded the %dMB cap; aborting without buffering "
                        "the rest (likely the wrong link, not a filing). Attempt %d/2.",
                        company_key, MAX_DOWNLOAD_BYTES // (1024 * 1024), attempt)
                    return None
                if len(buf) >= len(magic) and not buf.startswith(magic):
                    log.warning(
                        "[%s] Response did not look like a %s (content-type=%s); likely a WAF "
                        "challenge or wrong link. Attempt %d/2.",
                        company_key, file_type, resp.headers.get("content-type"), attempt)
                    return None
            if not buf.startswith(magic):
                # Response finished before ever reaching len(magic) bytes -
                # the chunked check above never ran, so check once more here.
                log.warning(
                    "[%s] Response (%d bytes) did not look like a %s; likely a WAF challenge "
                    "or wrong link. Attempt %d/2.", company_key, len(buf), file_type, attempt)
                return None
            return bytes(buf)


# 4. Download the resolved file, with browser headers + a WAF-challenge check,
# then verify its content actually covers the requested quarter.
async def download_file(company_key: str, download_url: str, file_type: str, target_dir: Path,
                        referer: str, cal_data: dict, dest_name: str | None = None) -> Path | None:
    ext = file_type.lower()
    filename = cfg.download_filename(company_key, ext)
    # dest_name lets several candidate URLs be validated concurrently without
    # racing each other for the same output path; the winner is renamed to the
    # canonical filename by the caller.
    dest = target_dir / (dest_name or filename)
    headers = {**BROWSER_HEADERS, "Referer": referer}
    magic = FILE_MAGIC[ext]

    async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=headers,
                                   verify=_ssl_context()) as http_client:
        for attempt in range(1, 3):
            try:
                content = await _fetch_bounded(http_client, download_url, magic, file_type,
                                               company_key, attempt)
            except httpx.HTTPError as e:
                log.warning("[%s] Download error on attempt %d/2: %s", company_key, attempt, e)
                content = None

            if content is None:
                if attempt == 2:
                    return None
                await asyncio.sleep(2)
                continue

            async with aiofiles.open(dest, "wb") as f:
                await f.write(content)

            if not validate_document(dest, ext, cal_data, company_key):
                log.warning("[%s] Discarded %s from %s.",
                            company_key, file_type, download_url)
                dest.unlink(missing_ok=True)
                # Terminal, not retried: a fully downloaded file that is the wrong
                # company/period/document will be the same wrong file next time,
                # and re-fetching it doubled the cost of every wrong candidate.
                return None

            log.info("[%s] Saved -> %s", company_key, dest)
            return dest

    return None


# NOTE on "ultra": it was previously the third rung here because it was the only
# processor observed to interact with tabs/category filters rather than just read
# rendered content (Aditya Birla's Download Center). It has been dropped from the
# default ladder on cost grounds. If ABHI (or any portal) regresses, give just
# that company "processors": ["core", "pro", "ultra"] in source_links.json - the
# capability is retained, it simply isn't billed for all seven companies. The
# URL-pattern cache also blunts this: once a company resolves even once, later
# quarters reuse its pattern and never dispatch an agent at all.


def gic_deterministic_url(cal_data: dict) -> str:
    """GIC's segment-wise report follows a fixed, verified path — no need to
    gamble on the agent for a mechanically-named file:
    /Industry Statistics/Segmentwise report/Year_{year}/segment_{month}_{year}.xlsx
    """
    month = cal_data["month"].lower()
    year = cal_data["year"]
    return (
        "https://www.gicouncil.in/Industry%20Statistics/Segmentwise%20report/"
        f"Year_{year}/segment_{month}_{year}.xlsx"
    )


_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# Documents on a disclosures page that are not the quarterly disclosure bundle.
_LISTING_NEGATIVE = ("stewardship", "voting", "annual report", "presentation",
                     "press", "brochure", "prospectus", "newsletter", "results",
                     "policy wording", "proxy")


def score_listing_link(href: str, text: str, cal_data: dict, fy: str, quarter: str) -> int:
    """How likely an <a> on a disclosures page is this period's disclosure PDF.
    0 means "not a candidate". The score only ORDERS candidates; every one is
    still downloaded and passed through validate_document."""
    path = urlsplit(href).path.lower()
    if not path.endswith(".pdf"):
        return 0
    blob = (unquote(href) + " " + text).lower()
    month = cal_data["month"].lower()
    abbrev = MONTH_ABBREV[month]
    year = str(cal_data["year"])
    q = quarter.lower()
    score = 0
    has_period = False
    if re.search(rf"(?<![a-z]){month}(?![a-z])", blob):
        score += 4
        has_period = True
    elif re.search(rf"(?<![a-z]){abbrev}(?![a-z])", blob):
        score += 3
        has_period = True
    if re.search(rf"(?<![a-z0-9]){q}(?![0-9])", blob.replace("_", " ").replace("-", " ")):
        score += 3
        has_period = True
    if not has_period:
        return 0
    if year in blob:
        score += 2
    if (cfg.fy_label(fy) in blob or cfg.fy_label_long(fy) in blob
            or f"fy {str(cal_data['year'])[-2:]}" in blob):
        score += 2
    if "disclosure" in blob:
        score += 1
    if any(neg in blob for neg in _LISTING_NEGATIVE):
        score -= 5
    return max(score, 0)


def extract_listing_links(html: str, base_url: str, cal_data: dict, fy: str,
                          quarter: str, limit: int = 5) -> list[str]:
    """Best-scoring PDF links on a disclosures page, best first, absolute."""
    best: dict[str, int] = {}
    for href, inner in _ANCHOR_RE.findall(html):
        href = unescape(href).strip()
        text = unescape(_TAG_RE.sub(" ", inner))
        absolute = urljoin(base_url, href)
        score = score_listing_link(absolute, text, cal_data, fy, quarter)
        if score > best.get(absolute, 0):
            best[absolute] = score
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    return [u for u, _ in ranked[:limit]]


async def scrape_listing_links(url: str, cal_data: dict, fy: str, quarter: str) -> list[str]:
    """Fetch the disclosures page directly and rank its PDF links. Resolves
    plain server-rendered portals in a second or two with no agent; a portal
    that builds its links with JavaScript yields nothing and falls through to
    the agent, as before."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, verify=_ssl_context(),
                                 headers={**BROWSER_HEADERS, "Accept": "text/html,*/*;q=0.8"}) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return extract_listing_links(resp.text, str(resp.url), cal_data, fy, quarter)


async def search_fallback(company_key: str, cal_data: dict, quarter: str, file_type: str, fy: str,
                          company: dict | None = None) -> list[str]:
    """Last resort when the Task agent can't resolve a link from the listing page
    itself (JS-tab-gated content, or the period isn't on the visible page at all):
    query Parallel's Search API, which draws on indexed/crawled web content rather
    than only what's reachable from the seed URL, and validate each candidate.
    https://docs.parallel.ai/search/search-quickstart
    """
    is_gic = company_key == "GIC"
    if is_gic:
        objective = f"Direct Excel download link for the GIC industry segment-wise report for {cal_data['month']} {cal_data['year']}"
        queries = [
            f"GIC segment wise report {cal_data['month']} {cal_data['year']} xlsx",
            "gicouncil.in segmentwise report download",
        ]
    else:
        # Full legal name (and registration number) rather than the config key,
        # so results are this insurer's filing and not a peer's or a news item.
        legal = (company or {}).get("name") or company_key
        reg = (company or {}).get("irdai_reg_no")
        who = f"{legal} (IRDAI registration {reg})" if reg else legal
        objective = (
            f"Direct PDF download link for {who}'s quarterly public disclosure "
            f"(IRDAI FORM NL schedules) for the quarter ended {cal_data['month']} {cal_data['year']} ({quarter})"
        )
        queries = [
            f"{legal} public disclosure quarter ended {cal_data['month']} {cal_data['year']} pdf",
            f"{legal} {quarter} FY {cfg.fy_label(fy)} public disclosure pdf",
        ]

    log.info("[%s] Querying Search API...", company_key)
    kwargs = {}
    # Opt-in per company ("allowed_domains" in source_links.json): the insurer's
    # own domain plus any CDN it publishes on. Unset keeps Search unrestricted,
    # because some filings live on an unrelated CDN host.
    allowed = (company or {}).get("allowed_domains")
    if allowed:
        kwargs["advanced_settings"] = {"source_policy": {"include_domains": list(allowed)}}
    result = await asyncio.wait_for(
        client.search(objective=objective, search_queries=queries, **kwargs),
        timeout=SEARCH_TIMEOUT_SECONDS,
    )
    # Don't filter by the insurer's own domain here: disclosure PDFs are often
    # hosted off-domain on a CDN (e.g. Star Health's live on a *.cloudfront.net
    # bucket, not starhealth.in) — that's exactly the case Search is meant to
    # cover when the Task agent, restricted to the seed domain, can't resolve it.
    # download_file's magic-bytes + validate_period checks do the real filtering.
    candidates = [r.url for r in result.results[:8]]
    log.info("[%s] Search candidates: %s", company_key, candidates)
    return candidates


# 5. Agentic Task Execution per company, with a bounded self-correcting retry:
# if the agent's link fails the file-integrity check in download_file, we re-run
# the task telling it the previous URL was invalid instead of silently giving up.
# A Search API fallback runs if the Task agent never resolves a valid link at all.
async def try_candidates_concurrently(company_key: str, candidate_urls: list[str], file_type: str,
                                      target_dir: Path, referer: str, cal_data: dict) -> Path | None:
    """Download and validate several candidate URLs at once, keeping the
    BEST-RANKED one that yields a genuine, correct-period document.

    Candidates arrive in the caller's priority order (Search relevance, or
    cached-pattern-before-fallback). Validating them concurrently is purely a
    speed measure - the choice between two that both validate must still go by
    rank, never by which download happened to finish first. An earlier version
    took the first to complete, which made the result depend on network timing:
    with a weak validator that let several unrelated PDFs pass, it could and
    did pick the wrong document."""
    if not candidate_urls:
        return None
    ext = file_type.lower()
    final_name = cfg.download_filename(company_key, ext)

    async def attempt(i, cand):
        try:
            return await download_file(
                company_key, cand, file_type, target_dir, referer=referer,
                cal_data=cal_data, dest_name=f".{final_name}.cand{i}")
        except Exception as e:
            log.warning("[%s] Candidate %s errored: %s", company_key, cand, e)
            return None

    results = await asyncio.gather(
        *[attempt(i, c) for i, c in enumerate(candidate_urls)])

    winner = None
    for cand, path in zip(candidate_urls, results):
        if path is None:
            continue
        if winner is None:
            winner = (path, cand)
        else:
            path.unlink(missing_ok=True)  # validated, but out-ranked
    if winner is None:
        return None
    path, cand = winner
    WINNING_URL[company_key] = cand
    final = target_dir / final_name
    path.replace(final)
    log.info("[%s] Saved -> %s (from %s)", company_key, final, cand)
    return final


async def process_company(company_key: str, data: dict, fy: str, quarter: str, target_dir: Path):
    url = data.get("quarterly_disclosure_page_link")
    cal_data = get_calendar_mapping(fy, quarter)
    is_gic = company_key == "GIC"
    file_type = "XLSX" if is_gic else "PDF"
    ext = file_type.lower()
    final_name = cfg.download_filename(company_key, ext)
    started = asyncio.get_event_loop().time()

    def record(method: str, detail: str = ""):
        ATTEMPT_LOG[company_key] = {
            "method": method,
            "detail": detail,
            "seconds": round(asyncio.get_event_loop().time() - started, 1),
        }

    # ---- Step 0: skip entirely if this period's file is already on disk ----
    dest = target_dir / final_name
    if dest.exists():
        if validate_document(dest, ext, cal_data, company_key):
            record("already-downloaded")
            log.info("[%s] %s already present for %s %s - retrieval done, skipping agent dispatch.",
                      company_key, final_name, quarter, fy)
            return
        # A wrong file saved by an earlier run would otherwise be trusted forever.
        rejected = dest.with_name(dest.name + ".rejected")
        rejected.unlink(missing_ok=True)
        dest.replace(rejected)
        log.warning("[%s] Existing %s failed validation; moved to %s and re-fetching.",
                    company_key, final_name, rejected.name)

    # ---- Step 1: cheap deterministic attempts, before any agent is billed ----
    # A cached pattern from a previous quarter, or GIC's known fixed path. Both
    # still go through download_file, so a stale pattern can't produce a wrong
    # file - it just fails and we fall through to the agent.
    direct_urls = []
    if is_gic:
        direct_urls.append(gic_deterministic_url(cal_data))
    cached = load_url_patterns().get(company_key)
    if cached:
        direct_urls.extend(render_url_template(cached, cal_data, quarter))

    if direct_urls:
        log.info("[%s] Trying %d direct URL(s) before dispatching an agent...",
                  company_key, len(direct_urls))
        dest = await try_candidates_concurrently(
            company_key, direct_urls, file_type, target_dir, referer=url, cal_data=cal_data)
        if dest is not None:
            record("direct-url", "gic-pattern" if is_gic else "cached-pattern")
            return
        log.info("[%s] No direct URL worked; falling back to the agent.", company_key)

    # ---- Step 1b: read the listing page ourselves before billing an agent ----
    # Server-rendered portals expose the PDF link in plain HTML. Candidates are
    # ranked by period tokens and each is still fully validated.
    if not is_gic and url:
        try:
            links = await scrape_listing_links(url, cal_data, fy, quarter)
        except Exception as e:
            log.info("[%s] Listing-page scrape unavailable (%s); falling back to the agent.",
                     company_key, e)
            links = []
        if links:
            log.info("[%s] Listing page offered %d candidate link(s); validating...",
                     company_key, len(links))
            dest = await try_candidates_concurrently(
                company_key, links, file_type, target_dir, referer=url, cal_data=cal_data)
            if dest is not None:
                winner = WINNING_URL.get(company_key)
                if winner:
                    save_url_pattern(company_key, winner, cal_data, quarter)
                record("listing-scrape")
                return
            log.info("[%s] No listing-page link validated; falling back to the agent.", company_key)

    ladder = ladder_for(data)

    async def run_processor(processor: str, feedback: str | None, dest_name: str | None = None):
        """Resolve + download for one processor. Returns (path, content).

        During the race, dest_name keeps this write off the canonical filename
        so it cannot interleave with the Search branch promoting its own temp
        file to the same path; the winner is promoted by the caller."""
        content, resolved_file_type = await resolve_download_url(
            company_key, url, cal_data, processor, fy, feedback, data)
        if content is None:
            return None, None
        path = await download_file(company_key, content["download_url"], resolved_file_type,
                                   target_dir, referer=url, cal_data=cal_data,
                                   dest_name=dest_name)
        return path, content

    async def run_search():
        candidates = await search_fallback(company_key, cal_data, quarter, file_type, fy, data)
        return await try_candidates_concurrently(
            company_key, candidates, file_type, target_dir, referer=url, cal_data=cal_data)

    # ---- Step 2: first processor raced against Search ----
    feedback = None
    searched = False
    if RACE_SEARCH_WITH_FIRST_PROCESSOR and ladder:
        searched = True
        log.info("[%s] Racing processor %r against the Search API...", company_key, ladder[0])
        proc_task = asyncio.create_task(
            run_processor(ladder[0], None, dest_name=f".{final_name}.agent"))
        search_task = asyncio.create_task(run_search())
        labels = {proc_task: f"processor:{ladder[0]}", search_task: "search"}
        pending = {proc_task, search_task}
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        outcome = task.result()
                    except Exception as e:
                        log.warning("[%s] %s failed: %s", company_key, labels[task], e)
                        continue
                    path = outcome[0] if isinstance(outcome, tuple) else outcome
                    if path is not None:
                        for other in pending:
                            other.cancel()
                        if task is proc_task:
                            # Promote the agent branch's temp file now that it
                            # has won uncontested.
                            path.replace(target_dir / final_name)
                            log.info("[%s] Saved -> %s", company_key, target_dir / final_name)
                            save_url_pattern(company_key, outcome[1]["download_url"], cal_data, quarter)
                        record(labels[task])
                        return
                    if task is proc_task and isinstance(outcome, tuple) and outcome[1] is not None:
                        feedback = _retry_feedback(outcome[1], cal_data)
        finally:
            for task in (proc_task, search_task):
                if not task.done():
                    task.cancel()
            # If the agent branch downloaded a file but Search got there first,
            # its temp file is now orphaned.
            (target_dir / f".{final_name}.agent").unlink(missing_ok=True)
        ladder = ladder[1:]

    # ---- Step 3: remaining processors, escalating ----
    for attempt, processor in enumerate(ladder, start=1):
        log.info("[%s] Dispatching Parallel AI agent -> %s (processor %s, %d/%d)",
                  company_key, url, processor, attempt, len(ladder))
        try:
            path, content = await run_processor(processor, feedback)
        except asyncio.TimeoutError:
            log.warning("[%s] Processor %s exceeded its %ds budget; escalating.",
                        company_key, processor, PROCESSOR_TIMEOUTS.get(processor, DEFAULT_PROCESSOR_TIMEOUT))
            continue
        except Exception as e:
            log.error("[%s] Agent task failed: %s", company_key, e)
            continue
        if path is not None:
            save_url_pattern(company_key, content["download_url"], cal_data, quarter)
            record(f"processor:{processor}")
            return
        if content is not None:
            feedback = _retry_feedback(content, cal_data)

    # ---- Step 4: Search, if it wasn't already raced ----
    if not searched:
        try:
            dest = await run_search()
        except Exception as e:
            log.warning("[%s] Search fallback failed: %s", company_key, e)
            dest = None
        if dest is not None:
            record("search")
            return

    record("failed")
    log.error("[%s] Giving up - no valid %s could be located or downloaded.", company_key, file_type)


def _retry_feedback(content: dict, cal_data: dict) -> str:
    return (
        f"IMPORTANT: A previous attempt returned {content['download_url']!r} "
        f"(titled {content.get('document_title')!r}), but that either failed to download "
        f"or, once downloaded, did not actually cover the quarter ended "
        f"{cal_data['month']} {cal_data['year']} (it may have been a different quarter's "
        f"file mislabeled or reused under this link). Do not repeat that URL or guess "
        f"another one by copying its naming pattern; find a link that is genuinely present "
        f"in the page's rendered content and confirm the document itself states the correct "
        f"quarter-ending date before returning it."
    )


async def main(fy: str, quarter: str, companies: list[str] | None = None):
    log.info("Starting Phase 1 Agentic Retrieval for %s %s...", quarter, fy)
    sources = load_sources()
    if companies is not None:
        selected = set(companies)
        unknown = selected - set(sources)
        if unknown:
            log.warning("Ignoring unknown company key(s): %s", sorted(unknown))
        sources = {k: v for k, v in sources.items() if k in selected}
    target_dir = ensure_download_dir(fy, quarter)

    started = asyncio.get_event_loop().time()
    tasks = [process_company(company_key, data, fy, quarter, target_dir) for company_key, data in sources.items()]
    await asyncio.gather(*tasks)
    elapsed = asyncio.get_event_loop().time() - started

    # Which route actually won, per company. Read this over a few quarters to
    # decide whether a processor still earns its place in the ladder - anything
    # that never appears here is pure cost and latency.
    summary_lines = [f"{'Company':<22}{'Resolved by':<22}{'Detail':<18}{'Seconds':>8}"]
    for company_key in sources:
        info = ATTEMPT_LOG.get(company_key, {"method": "not run", "detail": "", "seconds": 0.0})
        summary_lines.append(f"{company_key:<22}{info['method']:<22}{info['detail']:<18}{info['seconds']:>8.1f}")
    log.info("Phase 1 retrieval summary (%.1fs wall clock):\n%s", elapsed, "\n".join(summary_lines))

    by_method = Counter(i["method"] for i in ATTEMPT_LOG.values())
    log.info("By route: %s", ", ".join(f"{m}={n}" for m, n in sorted(by_method.items())))
    failed = [c for c, i in ATTEMPT_LOG.items() if i["method"] == "failed"]
    if failed:
        log.warning("FAILED (%d): %s", len(failed), ", ".join(failed))
    log.info("Phase 1 Agentic Execution Complete.")


if __name__ == "__main__":
    logging_setup.configure()
    TARGET_FY = "FY25-26"
    TARGET_QUARTER = "Q3"
    asyncio.run(main(TARGET_FY, TARGET_QUARTER))
