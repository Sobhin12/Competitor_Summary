"""Deterministic readers for schedules whose answer is a plain labelled-row
lookup, so they need not be sent to Gemini.

Each reader takes a company's cached PDF JSON (pdf_cache.get_company_json) and
returns {metric_key: {"fy26_q3", "fy25_q3", "found", "source_form",
"page_number", "evidence", "notes"}} - the same shape a Gemini answer has - for
ONLY the metrics it could resolve with confidence. Anything it is unsure about
is simply left out, and the caller sends just those metrics to Gemini. So a
reader may be incomplete but must never guess: a value it returns has to equal
what the filing prints.
"""
import re

from competitor_analysis import logging_setup
from competitor_analysis.extraction import pdf_cache
from competitor_analysis.extraction.forms import (FormPage, _nl36_blocks, classify_period_columns,
                                                  merge_row_values, parse_num, year_frags)
from competitor_analysis.extraction.schemas import STATES

log = logging_setup.get_logger(__name__)

_NUM_CELL = re.compile(r"^\(?-?[\d][\d,\s]*(?:\.\d+)?\)?$")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower().replace("&", "and"))


def _cells(row):
    return [" ".join(str(c).split()) if c is not None else "" for c in row]


def _is_number(cell):
    return cell in ("-", "") or bool(_NUM_CELL.match(cell))


_OTHERS_LABELS = ("others", "other", "otherstates", "allotherstates", "otherterritories",
                  "otherstatesuts", "allothers")


def _not_found(note):
    return {"fy26_q3": None, "fy25_q3": None, "found": False, "source_form": None,
            "page_number": None, "evidence": None, "notes": note}


def _answer(cur, form_name, page_number, evidence, prior=None, notes=None):
    return {"fy26_q3": cur, "fy25_q3": prior, "found": True,
            "source_form": form_name, "page_number": page_number,
            "evidence": evidence, "notes": notes}


# --- NL-34 Geographical Distribution: one row per state -------------------

# Label spellings seen across filings, folded onto schemas.STATES' names.
_STATE_ALIASES = {
    "jammuandkashmir": "Jammu & Kashmir", "jammukashmir": "Jammu & Kashmir",
    "nctofdelhi": "Delhi", "delhi": "Delhi", "newdelhi": "Delhi",
    "govtofnctofdelhi": "Delhi", "delhinct": "Delhi",
    "orissa": "Odisha", "pondicherry": "Puducherry",
    "andamanandnicobarislands": "Andaman and Nicobar Islands",
    "andamanandnicobar": "Andaman and Nicobar Islands",
    "damananddiu": "Daman & Diu",
}
_STATE_BY_NORM = {_norm(s): s for s in STATES if s != "Others"}
_STATE_BY_NORM.update({k: v for k, v in _STATE_ALIASES.items()})


def _is_blank_row(row):
    cells = _cells(row)
    at = next((i for i, c in enumerate(cells) if re.search(r"[A-Za-z]", c)), None)
    return at is None or not any(cells[at + 1:])


def _exact_rows(fp, wanted_norm, label_cols=3):
    """[(row, table_idx, row_idx)] for rows whose label cell equals
    `wanted_norm` after normalisation. Exact, not substring: "Delhi" must not
    also hit "New Delhi (Others)" and "Goa" must not hit "Goa & Daman"."""
    hits = []
    for ti, table in enumerate(fp.tables):
        for ridx, row in enumerate(table):
            cells = _cells(row)
            label = next((c for c in cells[:label_cols] if re.search(r"[A-Za-z]", c)), None)
            if label is not None and _norm(label) == wanted_norm:
                hits.append((row, ti, ridx))
    return hits


_UPTO_RE = re.compile(r"up\s*to", re.IGNORECASE)


def _yearless_total_column(table):
    """Column index of the company-Total "Up to the quarter" column for a
    table whose headers name NO year (Care, ManipalCigna print the period
    only in the page title), or None unless the header structure is exactly
    that: a sub-header row whose rightmost labelled cell is "Up to the
    quarter" under a group header whose rightmost labelled cell says
    "Total"."""
    for ridx, row in enumerate(table[:8]):
        cells = _cells(row)
        labelled = [i for i, c in enumerate(cells) if c]
        if not labelled or not _UPTO_RE.search(cells[labelled[-1]]):
            continue
        col = labelled[-1]
        for above in table[:ridx][::-1]:
            ac = _cells(above)
            group = [i for i, c in enumerate(ac) if c]
            if group and group[-1] >= col - 1 and re.search(r"total", ac[group[-1]], re.IGNORECASE):
                return col
        return None
    return None


def read_nl34(doc):
    """{'state_<name>': answer} for each state whose current AND prior
    cumulative Total were both read off the table's own period headers
    (forms.merge_row_values - the same column resolution the income-statement
    reads use). A state with either figure unresolved is left out for the
    model rather than reported half-filled."""
    pages = pdf_cache.pages_for_form(doc, "NL-34", max_pages=2)
    tables = [t for p in pages for t in p["tables"]]
    if not tables:
        return {}
    fp = FormPage(tables)
    cur_frag, prior_frag = year_frags()
    # Some filings (NBHI) print the current year only. That is a property of
    # the schedule, not a failed read: no cumulative header names the prior
    # year anywhere, so a null prior is the right answer. Where such a header
    # DOES exist, a prior that came back null is a failed read - left to the
    # model.
    prior_expected = any(f in text for ti in range(len(fp.tables))
                         for _, _, text in fp._events(ti) for f in prior_frag)
    page_no = pages[0]["page_number"]
    headers_name_a_year = any(f in text for ti in range(len(fp.tables))
                              for _, _, text in fp._events(ti)
                              for f in cur_frag + prior_frag)
    out = {}
    for norm_label, state in _STATE_BY_NORM.items():
        rows = _exact_rows(fp, norm_label)
        # A row printed with no figures at all (Narayana lists every state but
        # fills few) is "nothing reported", which the model has been recording
        # as not-found rather than 0 - not something to turn into a zero here.
        if not rows or all(_is_blank_row(r) for r, _, _ in rows):
            continue
        if headers_name_a_year:
            cur, prior = merge_row_values(fp, rows, cur_frag, prior_frag)
        else:
            row, ti, _ = rows[0]
            col = _yearless_total_column(fp.tables[ti])
            if col is None or col >= len(row):
                continue
            cur, prior = parse_num(row[col]), None
        if cur is None or (prior is None and prior_expected):
            continue
        out[f"state_{state}"] = _answer(
            cur, "Geographical Distribution of Business (NL-34)", page_no,
            f"{state}: {cur:g}" + ("" if prior is None else f" / {prior:g}"), prior=prior)
    # "Others" is a residual the schedule almost never prints (the model has
    # answered not-found for it in every filing checked). Report the same
    # when no row is labelled like it; if one exists, the model decides.
    if not any(_exact_rows(fp, n) for n in _OTHERS_LABELS):
        out["state_Others"] = _not_found("no 'Others' state row present in NL-34")
    return out


# --- NL-36 Business - Channels Wise: premium and policy count per channel ---

# metric2 (as in schemas.CHANNELS_36) -> normalised label fragments, the first
# of which to match a row wins. Insurers punctuate these labels inconsistently.
# Direct Business is deliberately absent: it is a header with indented sub-rows
# whose treatment differs per filing (and the model itself is inconsistent on
# it), so it stays with Gemini.
_NL36_ROW_LABELS = {
    "Individual Agents": ("individualagent",),
    "Corporate Agents - Banks": ("corporateagentsbank", "corporateagentbank"),
    "Corporate Agents - Others": ("corporateagentsother", "corporateagentother"),
    "Brokers": ("broker",),
    "CSC": ("commonservicecent", "csc"),
    "IMF": ("insurancemarketing", "imf"),
    "Web Aggregator": ("webaggregator",),
    "POS": ("pointofsale",),
}


def _nl36_label(row):
    return _norm(next((c for c in _cells(row)[:3] if re.search(r"[A-Za-z]", c)), ""))


def read_nl36(doc):
    """{'channel_premium_<ch>' / 'channel_policies_<ch>': answer} for every
    channel row found, with the cumulative current/prior columns taken from
    the schedule's own two-row header (forms._nl36_blocks - it also copes with
    Narayana printing Premium before Policies). A channel whose figures can't
    be read off both periods is left out for the model."""
    pages = pdf_cache.pages_for_form(doc, "NL-36", max_pages=1)
    if not pages or not pages[0]["tables"]:
        return {}
    page = pages[0]
    table = max(page["tables"], key=len)
    out = {}
    for sub, prefix in (("premium", "channel_premium_"), ("policies", "channel_policies_")):
        cur_col, prior_col = _nl36_blocks(table, sub=sub)
        if cur_col is None or prior_col is None:
            continue
        for metric2, fragments in _NL36_ROW_LABELS.items():
            idx = next((i for i, row in enumerate(table)
                        if any(f in _nl36_label(row) for f in fragments)), None)
            if idx is None:
                continue
            cells = _cells(table[idx])
            raw = [cells[c] if c < len(cells) else "" for c in (cur_col, prior_col)]
            if any(c and not _is_number(c) for c in raw):
                continue
            if any(c == "" for c in raw):
                continue  # a blank cell is "not reported", not 0: the model's call
            cur, prior = parse_num(raw[0]), parse_num(raw[1])
            if cur is None or prior is None:
                continue
            out[f"{prefix}{metric2}"] = _answer(
                cur, "Business - Channels Wise (NL-36)", page["page_number"],
                f"{cells[1] if len(cells) > 1 else ''}: {raw[0]} / {raw[1]}", prior=prior)
    return out


# --- NL-6 Commission Schedule: commission by channel, and reinsurance ceded ---

_NL6_ROW_PREFIXES = {
    "Individual Agents": ("individualagent",),
    "Corporate Agents - Banks": ("corporateagentsbank", "corporateagentbank"),
    "Corporate Agents - Others": ("corporateagentsother", "corporateagentother"),
    "Brokers": ("insurancebroker", "broker"),
    "CSC": ("commonservicecent",),
    "IMF": ("insurancemarketingfirm",),
    "Web Aggregator": ("webaggregator",),
    "POS": ("pointofsale",),
}
_NL6_RI_CEDED_PREFIX = "lesscommissiononreinsuranceceded"


def _join_continuations(tables):
    """Star and Galaxy print NL-6's channel break-up as a second table with no
    header of its own, directly under the table that has it. Appending such a
    table's rows to the one above restores the header-above-rows shape every
    column lookup assumes (the channel rows are simply shorter, which the
    lookups already tolerate). Tables that carry their own period header are
    never merged."""
    joined = []
    for table in tables:
        has_header = any(e["period"].endswith(("cumulative", "quarter", "snapshot"))
                         for e in classify_period_columns(table))
        if not has_header and joined:
            joined[-1] = joined[-1] + table
        else:
            joined.append(table)
    return joined


def read_nl6(doc):
    """{'commission_ch_<ch>': answer, 'ri_commission': answer}. Rows are
    matched by label prefix and read through forms.merge_row_values, so the
    stacked current-year / prior-year blocks NL-6 prints on one page resolve
    from their own headers. Both periods must resolve, else the metric is
    left to the model. Direct Business is left to the model as in NL-36."""
    pages = pdf_cache.pages_for_form(doc, "NL-6", max_pages=2)
    tables = _join_continuations([t for p in pages for t in p["tables"]])
    if not tables:
        return {}
    fp = FormPage(tables)
    cur_frag, prior_frag = year_frags()
    page_no = pages[0]["page_number"]

    def rows_with_prefix(prefixes):
        return [(row, ti, ridx) for ti, table in enumerate(fp.tables)
                for ridx, row in enumerate(table)
                if any(_nl6_label(row).startswith(p) for p in prefixes)]

    out = {}
    wanted = [(f"commission_ch_{m2}", pre) for m2, pre in _NL6_ROW_PREFIXES.items()]
    wanted.append(("ri_commission", (_NL6_RI_CEDED_PREFIX,)))
    for key, prefixes in wanted:
        rows = rows_with_prefix(prefixes)
        if not rows:
            continue
        cur, prior = merge_row_values(fp, rows, cur_frag, prior_frag)
        if cur is None or prior is None:
            continue
        out[key] = _answer(cur, "Commission Schedule (NL-6)", page_no,
                           f"{_cells(rows[0][0])[0]}: {cur:g} / {prior:g}", prior=prior)
    return out


def _nl6_label(row):
    return _norm(next((c for c in _cells(row)[:2] if re.search(r"[A-Za-z]", c)), ""))


# --- NL-29 Debt Securities: rating and residual-maturity mix (Book Value %) ---

_PCT_CELL = re.compile(r"^\(?-?\d+(?:\.\d+)?\)?\s*%$")
_RATING_ROWS = {
    "AAA rated": ("aaarated", "aaa"),
    "AA or better": ("aaorbetter",),
    "Rated below AA but above A": ("ratedbelowaabutabovea",),
}
_MATURITY_ROWS = {
    "Up to 1 year": ("upto1year",),
    "More than 1 year and upto 3 years": ("morethan1yearandupto3years",),
    "More than 3 years and upto 7 years": ("morethan3yearsandupto7years",),
    "More than 7 years and upto 10 years": ("morethan7yearsandupto10years",),
    "Above 10 years": ("above10years",),
}


def _pct(cell):
    """'32%' -> 32.0, '-' -> 0.0, anything else (incl. a bare number) -> None."""
    cell = cell.strip()
    if cell == "-":
        return 0.0
    if not _PCT_CELL.match(cell):
        return None
    return parse_num(cell.replace("%", ""))


def _book_value_pct_columns(table):
    """(cur_col, prior_col): the Book Value block's "as % of total for this
    class" columns for the current and prior date. None, None unless the
    headers name both dates (a year is the only thing that can tell them
    apart) and a Book Value group."""
    cur_frag, prior_frag = year_frags()
    group_row = book_start = None
    for ridx, row in enumerate(table[:8]):
        for cidx, c in enumerate(_cells(row)):
            if "bookvalue" in _norm(c) and len(_cells(row)[cidx]) < 30:
                group_row, book_start = ridx, cidx
    if book_start is None:
        return None, None
    market_after = [cidx for cidx, c in enumerate(_cells(table[group_row]))
                    if cidx > book_start and "marketvalue" in _norm(c)]
    end = market_after[0] if market_after else len(table[group_row])
    for row in table[group_row + 1:group_row + 3]:
        cells = _cells(row)
        cur = prior = None
        for cidx in range(book_start, min(end, len(cells))):
            text = cells[cidx]
            if re.search(r"as\s+(?:at|on)", text, re.I) and cidx + 1 < len(cells) \
                    and "oftotal" in _norm(cells[cidx + 1]):
                if any(f in text for f in cur_frag):
                    cur = cidx + 1
                elif any(f in text for f in prior_frag):
                    prior = cidx + 1
        if cur is not None and prior is not None:
            return cur, prior
    return None, None


def read_nl29(doc):
    """{'debt_rating_<bucket>' / 'debt_maturity_<bucket>': answer} - Book Value
    "% of total" for both dates. Sections are cut by their own Total rows; a
    bucket whose row is absent (Narayana omits empty maturity rows) or whose
    cells aren't percentages is left to the model."""
    pages = pdf_cache.pages_for_form(doc, "NL-29", max_pages=2)
    tables = [t for p in pages for t in p["tables"]]
    if not tables:
        return {}
    page_no = pages[0]["page_number"]
    table = max(tables, key=len)
    cur_col, prior_col = _book_value_pct_columns(table)
    if cur_col is None:
        return {}
    labels = [_norm(next((c for c in _cells(r)[:2] if re.search(r"[A-Za-z]", c)), "")) for r in table]
    # Sections are cut at their own "Breakdown ..." headings (not at Total rows:
    # ABHI prints none). Rating runs up to the residual-maturity heading,
    # maturity up to the next heading or Total row.
    marker = next((i for i, l in enumerate(labels) if l.startswith("breakdownbyresidual")), None)
    if marker is None:
        return {}
    rating_end = marker
    maturity_end = next((i for i in range(marker + 1, len(labels))
                         if labels[i].startswith(("breakdownby", "total", "note"))), len(labels))

    def pair(i):
        cells = _cells(table[i])
        if max(cur_col, prior_col) >= len(cells):
            return None
        cur, prior = _pct(cells[cur_col]), _pct(cells[prior_col])
        return None if cur is None or prior is None else (cur, prior)

    def find(lo, hi, prefixes):
        return next((i for i in range(lo, hi) if any(labels[i].startswith(p) for p in prefixes)), None)

    # A prior-date column whose Total is nil (a company with no prior-year
    # book yet) is "no comparative", which the model reports as null rather
    # than 0% for every bucket - leave the whole schedule to it.
    section = [pair(i) for i in range(rating_end)]
    if not any(p and p[1] for p in section):
        return {}
    out = {}

    def put(key, values, form_label):
        out[key] = _answer(values[0], "Detail Regarding Debt Securities (NL-29)", page_no,
                           f"{form_label}: {values[0]:g}% / {values[1]:g}%", prior=values[1])

    for bucket, prefixes in _RATING_ROWS.items():
        i = find(0, rating_end, prefixes)
        v = pair(i) if i is not None else None
        if v:
            put(f"debt_rating_{bucket}", v, bucket)
    # Sovereign is printed under many labels ("Any other (Sovereign)", "Any
    # other - Govt Securities", "1) Sovereign"): the first row of the rating
    # section that says sovereign, or else the "Any other" row.
    i = next((j for j in range(rating_end) if "sovereign" in labels[j]), None)
    if i is None:
        i = find(0, rating_end, ("anyother",))
    v = pair(i) if i is not None else None
    if v:
        put("debt_rating_Sovereign", v, "Sovereign")
    # "Rated below A" is the sum of its two sub-rows when it is not one row.
    single = next((i for i in range(rating_end) if labels[i] == "ratedbelowa"), None)
    below_b = find(0, rating_end, ("ratedbelowb",))
    but_b = find(0, rating_end, ("ratedbelowabutaboveb",))
    if single is not None and single not in (but_b, below_b):
        v = pair(single)
    elif but_b is not None or below_b is not None:
        parts = [pair(i) for i in (but_b, below_b) if i is not None]
        v = (sum(p[0] for p in parts), sum(p[1] for p in parts)) if all(parts) else None
    else:
        v = None
    if v:
        put("debt_rating_Rated below A", v, "Rated below A")
    for bucket, prefixes in _MATURITY_ROWS.items():
        i = find(marker + 1, maturity_end, prefixes)
        v = pair(i) if i is not None else None
        if v:
            put(f"debt_maturity_{bucket}", v, bucket)
    return out


# --- NL-41 Offices Information: point-in-time counts -----------------------

_LETTERED_NAME = re.compile(r"\(([a-z])\)\s*([^()]*)")
_LETTERED_NUMBER = re.compile(r"\(([a-z])\)\s*[A-Za-z\-: ]*?\s*((?<![A-Za-z])-(?![A-Za-z\d])|\d[\d,\s]*\d|\d)")
_NL41_INTERMEDIARIES = {
    "intermediary_Individual Agents": ("individualagent",),
    "intermediary_CA-Banks": ("corporateagentsbank", "corporateagentbank"),
    "intermediary_CA-Others": ("corporateagentsother", "corporateagentother"),
    "intermediary_Brokers": ("insurancebroker",),
    "intermediary_WA": ("webaggregator",),
    "intermediary_IMF": ("insurancemarketingfirm",),
    "intermediary_POS": ("pointofsale",),
}


def _count(text):
    """A whole-cell integer ('2 ,38,517' -> 238517); None for anything else."""
    text = text.strip()
    if text == "-":
        return 0.0
    return parse_num(text) if _NUM_CELL.match(text) else None


def read_nl41(doc):
    """Headcounts and office counts off NL-41. Items arrive two ways: packed
    into one row ("(a) Individual Agents (b) Corporate Agents-Banks ..." over
    "(a) 2,38,517 (b) 28 ...", matched letter to letter) or as one sub-row per
    lettered name. Anything not matched unambiguously is left to the model."""
    pages = pdf_cache.pages_for_form(doc, "NL-41", max_pages=2)
    if not pages:
        return {}
    page_no = pages[0]["page_number"]
    named = {}  # normalised name -> count, first sighting wins
    offices = None
    for page in pages:
        for table in page["tables"]:
            for row in table:
                cells = _cells(row)
                texts = [c for c in cells if c]
                if len(texts) < 2:
                    continue
                label, value = " ".join(texts[:-1]), texts[-1]
                packed = _LETTERED_NAME.findall(label)
                if len(packed) >= 2:
                    nums = dict(_LETTERED_NUMBER.findall(value))
                    for letter, name in packed:
                        if letter in nums and _count(nums[letter]) is not None:
                            named.setdefault(_norm(name), _count(nums[letter]))
                    continue
                m = _LETTERED_NAME.search(label)
                if m and _count(value) is not None:
                    named.setdefault(_norm(m.group(2)), _count(value))
                elif _norm(label).endswith("branchesattheendoftheyear") and _count(value) is not None:
                    offices = offices if offices is not None else _count(value)
                elif re.search(r"no\.?\s*of\s*branches\s+at\s+the\s+end", label, re.I) \
                        and _count(value) is not None and offices is None:
                    offices = _count(value)
    out = {}

    def put(key, count, form_label):
        out[key] = _answer(count, "Offices Information (NL-41)", page_no,
                           f"{form_label}: {count:g}")

    def named_count(prefixes):
        return next((v for k, v in named.items() if any(k.startswith(p) for p in prefixes)), None)

    for key, prefixes in (("employees_onroll", ("onroll",)),
                          ("agents_individual", ("individualagent",))):
        v = named_count(prefixes)
        if v is not None:
            put(key, v, key)
    for key, prefixes in _NL41_INTERMEDIARIES.items():
        v = named_count(prefixes)
        if v is not None:
            put(key, v, key)
    # A nil branch count is not trusted (a company can print "-" on the
    # branches row yet list offices elsewhere on the form): the model's call.
    if offices:
        put("offices_count", offices, "branches at the end of the year")
    return out


# --- NL-7 operating expenses and NL-20 solvency: cumulative line items ------

_NL7_ROWS = {
    "manpower_cost": ("employeesremuneration",),
    "it_spend": ("informationtechnology",),
    "rent_expense": ("rentsratesandtaxes", "rentratesandtaxes", "rentsratestaxes", "rentratestaxes"),
}


def _line_items(doc, form, rows_wanted, form_name, max_pages=2, require_prior=True):
    """{key: answer} for labelled rows read through forms.merge_row_values
    (cumulative current/prior columns resolved from the headers' own dates).
    `rows_wanted` is {metric_key: label prefixes}. A label matching more than
    one distinct row, or a period that can't be resolved, is left out."""
    pages = pdf_cache.pages_for_form(doc, form, max_pages=max_pages)
    tables = [t for p in pages for t in p["tables"]]
    if not tables:
        return {}
    fp = FormPage(tables)
    cur_frag, prior_frag = year_frags()
    page_no = pages[0]["page_number"]
    out = {}
    for key, prefixes in rows_wanted.items():
        hits = [(row, ti, ridx) for ti, table in enumerate(fp.tables)
                for ridx, row in enumerate(table)
                if any(_nl6_label(row).startswith(p) for p in prefixes)]
        if not hits:
            continue
        # One row per table is expected (a stacked prior-year block is its own
        # table); two in the same table is an ambiguous label.
        per_table = [ti for _, ti, _ in hits]
        if len(per_table) != len(set(per_table)):
            continue
        cur, prior = merge_row_values(fp, hits, cur_frag, prior_frag)
        if cur is None or (require_prior and prior is None):
            continue
        out[key] = _answer(cur, form_name, page_no,
                           f"{_cells(hits[0][0])[1] if len(hits[0][0]) > 1 else ''}: {cur:g}"
                           + ("" if prior is None else f" / {prior:g}"), prior=prior)
    return out


def read_nl7(doc):
    out = _line_items(doc, "NL-7", _NL7_ROWS, "Operating Expenses Schedule (NL-7)")
    # An insurer with no IT spend at all is implausible; a zero here is far
    # more likely a parent/heading row whose figures sit on sub-rows (seen on
    # Narayana), so the model decides.
    it = out.get("it_spend")
    if it and not it["fy26_q3"] and not it["fy25_q3"]:
        del out["it_spend"]
    return out


def read_nl20(doc):
    """Solvency ratio as the plain multiple the filing prints; a cell that
    carries a '%' is a different convention the model must adjudicate."""
    got = _line_items(doc, "NL-20", {"solvency_ratio": ("availablesolvencymargin",)},
                      "Analytical Ratios Schedule (NL-20)", max_pages=1)
    pages = pdf_cache.pages_for_form(doc, "NL-20", max_pages=1)
    if got and any("%" in str(c) for t in pages[0]["tables"][:1] for row in t
                   if "solvency" in " ".join(_cells(row)).lower() for c in row):
        return {}
    return got


# --- NL-3 balance sheet and NL-12 investments: "As at <date>" snapshots -----

def _snapshot_columns(table):
    """(cur_col, prior_col) of the first current/prior "As at <date>" header
    pair, from forms.classify_period_columns; None, None if either is absent."""
    cur = prior = None
    for ev in classify_period_columns(table):
        if ev["forward_filled"]:
            continue
        if ev["period"] == "current_snapshot" and cur is None:
            cur = ev["col"]
        elif ev["period"] == "prior_snapshot" and prior is None:
            prior = ev["col"]
    return cur, prior


_NL3_ROWS = {
    "capital": ("sharecapital",),
    "bs_reserves_surplus": ("reservesandsurplus",),
    "bs_debit_balance_pl": ("debitbalanceinprofit",),
}


def read_nl3(doc):
    """Capital, reserves and the P&L debit balance off the Balance Sheet's
    "As at" columns. A row printed '-' is a genuine zero; an absent row is
    left to the model."""
    pages = pdf_cache.pages_for_form(doc, "NL-3", max_pages=1)
    if not pages:
        return {}
    out = {}
    for table in pages[0]["tables"]:
        cur_col, prior_col = _snapshot_columns(table)
        if cur_col is None or prior_col is None:
            continue
        for key, prefixes in _NL3_ROWS.items():
            hits = [row for row in table
                    if any(_nl6_label(row).startswith(p) for p in prefixes)]
            if len(hits) != 1 or key in out:
                continue
            cells = _cells(hits[0])
            raw = [cells[c] if c < len(cells) else "" for c in (cur_col, prior_col)]
            if any(c == "" or not _is_number(c) for c in raw):
                continue
            cur, prior = parse_num(raw[0]), parse_num(raw[1])
            if cur is None or prior is None:
                continue
            out[key] = _answer(cur, "Balance Sheet (NL-3-B-BS)", pages[0]["page_number"],
                               f"{_cells(hits[0])[0]}: {raw[0]} / {raw[1]}", prior=prior)
        break
    return out


_NL12_GROUPS = {"aum_total": "total", "aum_shareholders": "shareholders",
                "aum_policyholders": "policyholders"}


def read_nl12(doc):
    """AUM from the Investment Schedule's GRAND TOTAL row, one column pair per
    fund group (Shareholders / Policyholders / Total). Each group's columns
    run from its heading to the next group's; the current/prior "As at" date
    inside that span picks the column."""
    pages = pdf_cache.pages_for_form(doc, "NL-12", max_pages=1)
    if not pages:
        return {}
    for table in pages[0]["tables"]:
        starts = {}
        for row in table[:5]:
            for cidx, c in enumerate(_cells(row)):
                if _norm(c) in ("shareholders", "policyholders", "total") and _norm(c) not in starts:
                    starts[_norm(c)] = cidx
        if len(starts) < 3:
            continue
        events = [e for e in classify_period_columns(table) if not e["forward_filled"]
                  and e["period"] in ("current_snapshot", "prior_snapshot")]
        order = sorted(starts.values())
        grand = [row for row in table if _nl6_label(row).startswith("grandtotal")]
        if len(grand) != 1:
            continue
        cells = _cells(grand[0])
        out = {}
        for key, group in _NL12_GROUPS.items():
            lo = starts[group]
            hi = next((s for s in order if s > lo), len(cells))

            def col(kind, lo=lo, hi=hi):
                return next((e["col"] for e in events if e["period"] == kind and lo <= e["col"] < hi), None)

            cc, pc = col("current_snapshot"), col("prior_snapshot")
            if cc is None or pc is None or max(cc, pc) >= len(cells):
                continue
            if not (_is_number(cells[cc]) and _is_number(cells[pc])) or "" in (cells[cc], cells[pc]):
                continue
            out[key] = _answer(parse_num(cells[cc]), "Investment Schedule (NL-12 & 12A)",
                               pages[0]["page_number"], f"GRAND TOTAL ({group}): {cells[cc]} / {cells[pc]}",
                               prior=parse_num(cells[pc]))
        return out
    return {}


READERS = (read_nl34, read_nl36, read_nl6, read_nl29, read_nl41, read_nl7, read_nl20, read_nl3,
           read_nl12)


def read_all(doc, wanted_keys=None):
    """Union of every reader's answers, optionally restricted to metric keys
    the caller actually asks for. A reader that raises is logged and skipped:
    its metrics simply fall through to the model, so a layout surprise can
    cost time but never the run."""
    out = {}
    for reader in READERS:
        try:
            got = reader(doc)
        except Exception as e:  # noqa: BLE001 - isolate one reader's failure
            log.warning("direct read %s failed (%s: %s) - leaving its metrics to the model.",
                        reader.__name__, type(e).__name__, e)
            continue
        out.update(got)
    if wanted_keys is not None:
        out = {k: v for k, v in out.items() if k in wanted_keys}
    return out
