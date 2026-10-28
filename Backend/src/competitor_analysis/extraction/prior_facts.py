"""
Prior-period facts: the ~25 PRIOR-column values (and last year-end's on-roll
headcount) that THIS period's filings do not carry - NL-37/39/41/45 have no
prior-year comparative - so they must come from last year's own filing.

They used to be read from an old Data_Engine_*.xlsx, picked as the newest file
by name. That file may be a partial or failed run, and a gap there passed
silently (blank prior cells; Slide 25's average headcount quietly falling back
to the closing count). Each fact is now resolved per company, per metric:

  1. last year's PDF, if on disk - extracted again (its pdf_json parse cache
     makes that cheap), unless the facts file already holds an extraction
     stamped with that exact PDF's hash, which is just a cache of this step;
  2. else the facts file, artifacts/cache/prior_facts/{FY}_{Q}.json;
  3. else, last resort, an old Data Engine workbook - but only one whose
     company block is substantially filled, tried newest-first, and never
     copied into the facts file.

Anything still missing stays empty and is reported - never substituted.

The facts file is written per company as each extraction finishes (the same
function records the CURRENT period after every company, which is what next
year's run reads), only non-empty values, never blanking an existing one,
atomically. A run that stops half-way therefore leaves correct facts for the
companies that completed and nothing for the rest.
"""
import glob
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from competitor_analysis import config as cfg
from competitor_analysis import paths

log = logging.getLogger(__name__)

# Bump when an extractor feeding PRIOR_BACKFILL_TARGETS changes its output:
# stored extractions with another version are redone rather than trusted.
EXTRACTOR_VERSION = "1"
ONROLL_KEY = "NL41_ONROLL_CLOSING"
# A workbook's company block must have at least this share of its rows filled
# in the CUR column to count as a completed run for that company.
MIN_WORKBOOK_FILL = 0.6

_lock = threading.Lock()
_ONROLL_MEMO = {}


def fact_key(slide, metric1, metric2=None):
    return f"{slide}|{metric1}|{metric2 or ''}"


def targets():
    from competitor_analysis.extraction.data_engine import PRIOR_BACKFILL_TARGETS
    return list(PRIOR_BACKFILL_TARGETS)


def facts_path(fy, quarter):
    return Path(paths.PRIOR_FACTS_DIR) / f"{fy}_{quarter}.json"


def _load(fy, quarter):
    path = facts_path(fy, quarter)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("companies"), dict):
            return doc
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.exception("prior facts file %s is unreadable - ignoring it", path)
    return {"fy": fy, "quarter": quarter, "companies": {}}


def _save(fy, quarter, doc):
    path = facts_path(fy, quarter)
    os.makedirs(path.parent, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _pdf_sha(pdf_path):
    from competitor_analysis.extraction import pdf_cache
    return pdf_cache._content_fingerprint(pdf_path)[0]


def record_facts(fy, quarter, company, facts, pdf_path, source):
    """Merge `facts` ({fact_key: value}) into the period's file and stamp the
    company as fully extracted from `pdf_path`. None values are dropped and an
    existing value is only ever replaced by another real one."""
    clean = {k: v for k, v in facts.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    stamp = {"pdf_sha256": _pdf_sha(pdf_path) if pdf_path and os.path.isfile(pdf_path) else None,
             "version": EXTRACTOR_VERSION, "source": source,
             "at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        doc = _load(fy, quarter)
        entry = doc["companies"].setdefault(company, {"facts": {}})
        entry.setdefault("facts", {}).update(clean)
        entry["_extracted"] = stamp
        _save(fy, quarter, doc)
    _ONROLL_MEMO.clear()


def record_current_facts(company, derived, income_rows, pdf_path):
    """Called after each company of a normal run: stores this period's CUR
    values for the backfill targets (plus the closing on-roll count), which
    become next year's PRIOR. `derived` is compute_derived_metrics' output,
    `income_rows` the company's extract_income_statement dict."""
    facts = {}
    for (slide, m1, m2) in targets():
        pair = derived.get((slide, m1, m2))
        if pair and pair[0] is not None:
            facts[fact_key(slide, m1, m2)] = pair[0]
    onroll = (income_rows or {}).get("NL-41 On-roll", (None, None))[0]
    if onroll:
        facts[ONROLL_KEY] = onroll
    record_facts(cfg.FY, cfg.QUARTER, company, facts, pdf_path, "pdf")


# ---------------------------------------------------------------------------
# Tier 3 - old Data Engine workbooks (validated, newest first)
# ---------------------------------------------------------------------------

def _read_workbook(path, fy, quarter):
    """{company: {fact_key: value}} for the companies whose block in this
    workbook looks like a completed run, or None if it isn't a usable Data
    Engine workbook for the period. Header-matched, so a workbook relabelled
    or saved part-way is judged by what it actually contains."""
    import openpyxl
    from competitor_analysis.extraction.data_engine import normalize_text
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        log.warning("prior workbook %s is unreadable - skipped", os.path.basename(path))
        return None
    try:
        if "Data Engine" not in wb.sheetnames:
            return None
        rows = wb["Data Engine"].iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows, ())]
        col = cfg.period_column(fy, quarter)
        need = ("Slide #", "Company", "Meric 1", "Metric 2", col)
        if any(h not in header for h in need):
            return None
        si, ci, m1i, m2i, vi = (header.index(h) for h in need)
        wanted = {(s, normalize_text(m1)): fact_key(s, m1, m2) for (s, m1, m2) in targets()}
        # The sheet's Company column holds the pipeline's own short keys.
        short = {normalize_text(k): k for k in cfg.COMPANY_PDF_FILENAMES}
        total, filled, found = {}, {}, {}
        for r in rows:
            comp = short.get(normalize_text(r[ci]))
            if not comp:
                continue
            total[comp] = total.get(comp, 0) + 1
            v = r[vi] if vi < len(r) else None
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            filled[comp] = filled.get(comp, 0) + 1
            try:
                slide = int(r[si])
            except (TypeError, ValueError):
                continue
            key = wanted.get((slide, normalize_text(r[m1i])))
            if key:
                found.setdefault(comp, {}).setdefault(key, v)
            if (str(r[m1i] or "").strip() == "Employees"
                    and str(r[m2i] or "").lower().startswith("on-roll")):
                found.setdefault(comp, {}).setdefault(ONROLL_KEY, v)
        return {c: f for c, f in found.items()
                if total.get(c) and filled.get(c, 0) / total[c] >= MIN_WORKBOOK_FILL}
    finally:
        wb.close()


def _workbook_facts(fy, quarter, companies):
    """{company: {fact_key: value}} from the newest acceptable workbook per
    company. One pass over the candidates, newest first."""
    out = {}
    pattern = str(paths.OUTPUT_DIR / f"Data_Engine_{fy}_{quarter}_*.xlsx")
    for path in sorted(glob.glob(pattern), reverse=True):
        if all(c in out for c in companies):
            break
        got = _read_workbook(path, fy, quarter)
        if not got:
            continue
        for c in companies:
            if c not in out and got.get(c):
                out[c] = dict(got[c], _file=os.path.basename(path))
    return out


# ---------------------------------------------------------------------------
# Tier 1 - extract from last year's PDF
# ---------------------------------------------------------------------------

def _extract_company(company, pdf_path):
    """Run this company's normal extraction against `pdf_path` under whatever
    period is active, and return {fact_key: value}. Caller owns the period and
    GIC lookup (see get_prior_facts)."""
    from competitor_analysis.extraction import data_engine as de
    inc = de.extract_income_statement(company, pdf_path)
    derived = de.build_derived(company, pdf_path, inc=inc,
                               opening_onroll=opening_onroll(company))
    facts = {}
    for (slide, m1, m2) in targets():
        pair = derived.get((slide, m1, m2))
        if pair and pair[0] is not None:
            facts[fact_key(slide, m1, m2)] = pair[0]
    onroll = inc.get("NL-41 On-roll", (None, None))[0]
    if onroll:
        facts[ONROLL_KEY] = onroll
    return facts


def get_prior_facts(companies, extract=True):
    """({company: {fact_key: value}}, {company: description of where it came
    from}) for this run's prior period (same quarter, previous FY)."""
    fy, quarter = cfg.prior_fy(), cfg.QUARTER
    doc = _load(fy, quarter)
    facts = {c: {} for c in companies}
    source = {}
    to_extract = []

    for c in companies:
        entry = doc["companies"].get(c) or {}
        pdf = cfg.expected_pdf_path(c, fy, quarter)
        if extract and os.path.isfile(pdf):
            st = entry.get("_extracted") or {}
            if st.get("pdf_sha256") == _pdf_sha(pdf) and st.get("version") == EXTRACTOR_VERSION:
                facts[c].update(entry.get("facts", {}))
                source[c] = "facts file (stamped with this PDF)"
            else:
                to_extract.append((c, pdf))
        elif entry.get("facts"):
            facts[c].update(entry["facts"])
            source[c] = "facts file (no PDF on disk)" if extract else "facts file"

    if to_extract:
        _extract_prior(fy, quarter, to_extract, facts, source)

    missing = [c for c in companies if not facts[c]]
    if missing:
        for c, got in _workbook_facts(fy, quarter, missing).items():
            file = got.pop("_file")
            facts[c].update(got)
            source[c] = f"workbook fallback ({file})"
    for c in companies:
        if not facts[c]:
            source[c] = "NOT FOUND - prior cells stay empty"
    return facts, source


def _extract_prior(fy, quarter, to_extract, facts, source):
    from competitor_analysis.extraction import data_engine as de
    with cfg.temporary_period(fy, quarter):
        gic = None
        if os.path.isfile(cfg.gic_path()):
            try:
                gic = de.GicData()
            except Exception:
                log.exception("prior-period GIC.xlsx unreadable - extracting without it")
        saved_lookup = de._GIC_GDPI_LOOKUP
        # The current period's GIC GDPI must not leak in as last year's GWP.
        de.set_gic_gdpi_lookup(de.gic_gdpi_lookup(gic) if gic else {})
        try:
            for company, pdf in to_extract:
                try:
                    log.info("prior period %s %s: extracting %s from its PDF", fy, quarter, company)
                    got = _extract_company(company, pdf)
                except Exception:
                    log.exception("prior-period extraction failed for %s - falling back", company)
                    continue
                record_facts(fy, quarter, company, got, pdf, "pdf")
                entry = _load(fy, quarter)["companies"].get(company, {})
                facts[company].update(entry.get("facts", got))
                source[company] = "extracted from last year's PDF"
        finally:
            de.set_gic_gdpi_lookup(saved_lookup)


# ---------------------------------------------------------------------------
# Opening on-roll headcount (last year-end's NL-41 count)
# ---------------------------------------------------------------------------

def opening_onroll(company, for_fy=None):
    """On-roll employees at the start of `for_fy` (default: the active FY) =
    the closing count of the previous FY's Q4. Same tiers as the other facts.
    None when no tier has it - the caller then uses the closing count alone,
    and this logs that it did."""
    if company not in cfg.COMPANY_PDF_FILENAMES:
        return None  # not one of the insurers this pipeline has filings for
    for_fy = for_fy or cfg.FY
    fy = cfg.prior_fy(for_fy)
    memo_key = (company, fy)
    if memo_key in _ONROLL_MEMO:
        return _ONROLL_MEMO[memo_key]
    value, how = None, None
    pdf = cfg.expected_pdf_path(company, fy, "Q4")
    entry = _load(fy, "Q4")["companies"].get(company) or {}
    if os.path.isfile(pdf):
        from competitor_analysis.extraction import data_engine as de
        try:
            with cfg.temporary_period(fy, "Q4"):
                value = de.extract_nl41_onroll(pdf)
        except Exception:
            log.exception("opening on-roll extraction failed for %s", company)
        if value:
            how = "PDF"
            _merge_onroll(fy, company, value)
    if not value and entry.get("facts", {}).get(ONROLL_KEY):
        value, how = entry["facts"][ONROLL_KEY], "facts file"
    if not value:
        got = _workbook_facts(fy, "Q4", [company]).get(company)
        if got and got.get(ONROLL_KEY):
            value, how = got[ONROLL_KEY], f"workbook fallback ({got['_file']})"
    if value:
        log.info("%s opening on-roll %s (from %s Q4 %s)", company, value, fy, how)
    else:
        log.warning("%s: no %s Q4 on-roll count found - Slide 25 falls back to the closing count",
                    company, fy)
    _ONROLL_MEMO[memo_key] = value
    return value


def _merge_onroll(fy, company, value):
    """Add just the on-roll fact, leaving the company's extraction stamp
    alone (it says nothing about this single fact)."""
    with _lock:
        doc = _load(fy, "Q4")
        entry = doc["companies"].setdefault(company, {"facts": {}})
        entry.setdefault("facts", {})[ONROLL_KEY] = value
        _save(fy, "Q4", doc)


def reset_memo():
    _ONROLL_MEMO.clear()
