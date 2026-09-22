"""
Multi-year trend tables for the report's "Historical Trends" slide(s).

Source is data/historical/Historical_Trends.xlsx (paths.HISTORICAL_TRENDS_WORKBOOK)
- a single sheet containing several stacked tables, one per metric, each
shaped like:

    Retail Health:
    Particulars (Rs in crs)   FY18   FY19   ...   FY26
    NBHI                       634    737   ...   5,748
    STAR                      3,629  4,678  ...  17,743
    ...
    <blank row>

Unlike every other slide in report.py (which reads Data_Engine_UI.xlsx's
2-column CUR/PRIOR shape via reporting.data), this file's tables carry a
full year-by-year series - a different shape entirely, hence a separate
loader rather than an extension of data.py.

Maintained by hand, once a year, and placed at that path directly (no
upload endpoint) - see storage/r2.py's restore_all(), which pulls it down
on a fresh container instance the same way as every other data/ input.
"""
import re
from pathlib import Path

import openpyxl

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis.reporting import theme

_YEAR_RE = re.compile(r"(\d{2,4})")

# Non-company rows worth keeping under their own key (e.g. slide_08's SAHI
# vs Industry growth panel) - everything else that doesn't resolve via
# theme.canonical_company() (state names, "Total", other aggregate labels)
# is silently skipped, same as before.
_AGGREGATE_ALIASES = {"sahi's": "SAHI", "sahi": "SAHI", "industry": "Industry"}


def _year_sort_key(label):
    m = _YEAR_RE.search(str(label))
    return int(m.group(1)) if m else 0


def _is_header_row(row):
    first = row[0] if row else None
    return isinstance(first, str) and first.strip().lower().startswith("particulars")


def sorted_years(table, cap_to_period=True):
    """table: {company_key: {year_label: value}} (one entry of
    load_historical_tables()'s return). Returns every year label seen across
    all companies, chronologically ordered.

    `cap_to_period` (default True) drops any year beyond the active
    reporting period's FY-end year (config.fy_end_year()). The workbook is
    updated by hand once a year, so it can run ahead of a quarterly report
    mid-FY (its latest column would be a not-yet-real future year the report
    period hasn't reached) or behind it (the workbook simply hasn't been
    updated for the current FY yet). Capping at the period's own FY-end year
    handles the first case; since that cap can only ever drop columns, not
    add ones the workbook doesn't have, whatever's left is naturally always
    the most recent year actually present - handling the second case too,
    with no special-casing needed."""
    years = {y for comp in table.values() for y in comp}
    if cap_to_period:
        cap = cfg.fy_end_year() % 100
        years = {y for y in years if _year_sort_key(y) <= cap}
    return sorted(years, key=_year_sort_key)


def cagr(table, cap_to_period=True):
    """{row_key: CAGR fraction} spanning the table's earliest year through
    its most recent one (capped to the active period the same way
    sorted_years() is, so the end year moves with the reporting period
    rather than being hardcoded). A key is omitted if either endpoint is
    missing or the start value isn't positive (CAGR is undefined for a
    zero/negative base)."""
    years = sorted_years(table, cap_to_period=cap_to_period)
    if len(years) < 2:
        return {}
    start_year, end_year = years[0], years[-1]
    n = _year_sort_key(end_year) - _year_sort_key(start_year)
    if n <= 0:
        return {}
    out = {}
    for key, series in table.items():
        sv, ev = series.get(start_year), series.get(end_year)
        if sv is None or ev is None or sv <= 0:
            continue
        out[key] = (ev / sv) ** (1 / n) - 1
    return out


def load_historical_tables(path=None):
    """Scans the sheet top-to-bottom for title -> header -> company/aggregate
    -row blocks, each terminated by a blank first cell or EOF (see
    _is_header_row for what makes a row a header).

    Returns {metric_title: {row_key: {year_label: value}}}. A title row is
    whatever non-empty text sits directly above the header row - trailing
    ':' is stripped. A row's key is theme.canonical_company() for a company
    row (any of the sheet's spelling variants resolve the same way they do
    everywhere else in the report), or its _AGGREGATE_ALIASES entry for a
    handful of recognized non-company rows (SAHI, Industry); anything else
    (state names, "Total", other aggregates) is silently skipped.

    Returns {} if the workbook doesn't exist yet (this data is optional -
    the slide that reads it just skips, per the report's usual
    no-data-no-placeholder rule) or is otherwise unreadable.
    """
    path = path or paths.HISTORICAL_TRENDS_WORKBOOK
    if not Path(path).is_file():
        return {}
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    # min_row/min_col anchored to the sheet's own used range (not hardcoded to
    # column A) - the real workbook's tables start at column B, with column A
    # entirely unused.
    rows = [[c.value for c in row]
            for row in ws.iter_rows(min_row=ws.min_row, min_col=ws.min_column)]

    tables = {}
    pending_title = None
    i, n = 0, len(rows)
    while i < n:
        row = rows[i]
        if _is_header_row(row):
            year_cols = []
            for ci in range(1, len(row)):
                v = row[ci]
                if v is None or not str(v).strip():
                    break
                year_cols.append((ci, str(v).strip()))
            title = (pending_title or "Untitled").strip().rstrip(":").strip()
            table = {}
            i += 1
            while i < n:
                r = rows[i]
                label = r[0] if r else None
                if label is None or not str(label).strip():
                    break
                key = theme.canonical_company(label) or _AGGREGATE_ALIASES.get(str(label).strip().lower())
                # An unrecognized label (e.g. "Total Health & PA") ends this
                # block rather than being silently skipped - otherwise the
                # loop reads straight through it into the NEXT table's title
                # and header rows, corrupting both (this table absorbs the
                # next one's data rows, and the next table's own header is
                # never seen by the outer loop, so it never gets recognized
                # at all). The outer loop's pending_title tracking already
                # handles skipping through non-header junk correctly.
                if key is None:
                    break
                table[key] = {yl: (r[ci] if ci < len(r) else None) for ci, yl in year_cols}
                i += 1
            tables[title] = table
            pending_title = None
            continue
        first = row[0] if row else None
        if first is None or not str(first).strip():
            pending_title = None
        else:
            pending_title = str(first).strip()
        i += 1
    return tables


def get_tables(titles, path=None):
    """Loads the workbook once and returns {requested_title: table_or_None}
    for each of `titles`, matched case/colon-insensitively against whatever
    title each block actually had in the sheet."""
    lookup = {t.lower(): tbl for t, tbl in load_historical_tables(path).items()}
    return {title: lookup.get(title.strip().rstrip(":").lower()) for title in titles}


def get_table(title, path=None):
    return get_tables([title], path)[title]
