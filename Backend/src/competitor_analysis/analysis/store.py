"""
FactStore: every Data Engine workbook in artifacts/output plus
Historical_Trends.xlsx, flattened into one list of facts

    (period, period_type, metric, dimension, entity) -> value

so the analysis tools can query across quarters and years instead of being
limited to one workbook's CUR/PRIOR pair.

Period ids:
  quarter  'FY26-27_Q1'  (year-to-date figures, as filed)
  annual   'FY25-26'     (full year; from Historical_Trends.xlsx, or a Q4
                          Data Engine's figures where the history lacks one)

Precedence when the same fact appears more than once: a period's own
workbook (as CUR) beats a later workbook's PRIOR column; a newer run of the
same period beats an older one; for annual figures the hand-maintained
history beats a Q4 Data Engine.

Workbooks are read positionally (the column layout is fixed, the two value
headers are period labels), so building the store never depends on the
global config.set_period().
"""
import glob
import os
import re
import threading
from dataclasses import dataclass

import openpyxl

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis.analysis import metrics as M

_TS_RE = re.compile(r"_(\d{8}_\d{6})\.xlsx$", re.IGNORECASE)
_PERIOD_RE = re.compile(r"^FY(\d{2})-(\d{2})(?:_Q([1-4]))?$")

# Data Engine column positions (0-based) - see extraction/data_engine.HEADERS.
_C_SLIDE, _C_CATEGORY, _C_COMPANY, _C_M1, _C_M2, _C_TAB, _C_LINK, _C_CUR, _C_PRIOR, _C_GROWTH = range(10)


@dataclass(frozen=True, slots=True)
class Fact:
    period: str
    period_type: str        # "quarter" | "annual"
    metric: str
    dimension: str | None
    entity: str
    value: float
    source: str             # workbook file name the value came from
    source_link: str | None = None

    @property
    def id(self):
        return f"{self.period}|{self.metric}|{self.dimension or ''}|{self.entity}"


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def make_period(fy, quarter=None):
    fy = cfg.normalize_fy(fy)
    return f"{fy}_{quarter}" if quarter else fy


def parse_period(period):
    """(fy_start_two_digit, quarter_int_or_None)."""
    m = _PERIOD_RE.match(period or "")
    if not m:
        raise ValueError(f"Unrecognised period {period!r} - expected e.g. 'FY26-27_Q1' or 'FY25-26'.")
    return int(m.group(1)), (int(m.group(3)) if m.group(3) else None)


def period_sort_key(period):
    start, q = parse_period(period)
    # An annual period sorts with (just after) its own Q4.
    return (start, q if q is not None else 4.5)


def prior_year_period(period):
    """Same quarter (or full year) one financial year earlier."""
    start, q = parse_period(period)
    fy = f"FY{(start - 1) % 100:02d}-{start % 100:02d}"
    return f"{fy}_Q{q}" if q else fy


def period_label(period):
    """'Q1 FY26-27' / 'FY25-26 (full year)'."""
    start, q = parse_period(period)
    fy = f"FY{start:02d}-{(start + 1) % 100:02d}"
    return f"Q{q} {fy}" if q else f"{fy} (full year)"


def period_type(period):
    return "annual" if parse_period(period)[1] is None else "quarter"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class FactStore:
    def __init__(self, facts, sources):
        self.facts = facts
        self.sources = sources            # [{"file", "period", "kind"}]
        self._by_metric = {}
        for f in facts:
            self._by_metric.setdefault(f.metric, []).append(f)
        self._by_id = {f.id: f for f in facts}

    # -- lookups ----------------------------------------------------------------
    def get(self, fact_id):
        return self._by_id.get(fact_id)

    def value(self, metric, entity, period, dimension=None):
        f = self._by_id.get(f"{period}|{metric}|{dimension or ''}|{entity}")
        return f.value if f else None

    def fact(self, metric, entity, period, dimension=None):
        return self._by_id.get(f"{period}|{metric}|{dimension or ''}|{entity}")

    def query(self, metric=None, entities=None, periods=None, dimension=..., period_type=None):
        """Facts matching every given filter. dimension=... means any;
        dimension=None means only the undimensioned value."""
        pool = self._by_metric.get(metric, []) if metric else self.facts
        ents = set(entities) if entities else None
        pers = set(periods) if periods else None
        return [f for f in pool
                if (ents is None or f.entity in ents)
                and (pers is None or f.period in pers)
                and (dimension is ... or f.dimension == dimension)
                and (period_type is None or f.period_type == period_type)]

    def periods(self, period_type=None, metric=None):
        pool = self._by_metric.get(metric, []) if metric else self.facts
        ps = {f.period for f in pool if period_type is None or f.period_type == period_type}
        return sorted(ps, key=period_sort_key)

    def latest_quarter(self):
        """Latest quarter that has its own Data Engine (not one only known as
        another workbook's PRIOR column)."""
        own = [s["period"] for s in self.sources if s["kind"] == "data_engine"]
        return max(own, key=period_sort_key) if own else None

    def metric_ids(self):
        return [m for m in M.METRICS if m in self._by_metric]

    def dimensions(self, metric):
        seen = []
        for f in self._by_metric.get(metric, []):
            if f.dimension is not None and f.dimension not in seen:
                seen.append(f.dimension)
        return seen

    def entities(self, metric=None):
        pool = self._by_metric.get(metric, []) if metric else self.facts
        found = {f.entity for f in pool}
        companies = [c for c in M.COMPANY_KEYS if c in found]
        return companies + sorted(found - set(companies))


def _latest_data_engines(output_dir):
    """{period: path} keeping the newest timestamped run per FY+Quarter."""
    best = {}
    for path in glob.glob(os.path.join(str(output_dir), "Data_Engine_*.xlsx")):
        parsed = cfg.parse_period_from_data_engine_path(path)
        ts = _TS_RE.search(path)
        if not parsed or not ts:
            continue
        period = make_period(*parsed)
        if period not in best or ts.group(1) > best[period][0]:
            best[period] = (ts.group(1), path)
    return {p: path for p, (_, path) in best.items()}


def _read_data_engine(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Data Engine"]
        return [r for r in ws.iter_rows(min_row=2, values_only=True)
                if r and isinstance(r[_C_SLIDE], int)]
    finally:
        wb.close()


def build_store(output_dir=None, historical_path=None):
    output_dir = output_dir or paths.OUTPUT_DIR
    candidates = {}        # fact id -> (priority tuple, Fact)
    sources = []

    def offer(fact, priority):
        cur = candidates.get(fact.id)
        if cur is None or priority < cur[0]:
            candidates[fact.id] = (priority, fact)

    for period, path in _latest_data_engines(output_dir).items():
        name = os.path.basename(path)
        ts = _TS_RE.search(name).group(1)
        prior = prior_year_period(period)
        is_q4 = parse_period(period)[1] == 4
        sources.append({"file": name, "period": period, "kind": "data_engine"})
        # Negative timestamp ordering: newer runs win ties within a rank.
        newer = -int(ts.replace("_", ""))
        for r in _read_data_engine(path):
            cls = M.classify(r[_C_CATEGORY], r[_C_COMPANY], r[_C_M1], r[_C_M2])
            if cls is None:
                continue
            metric, dim, entity = cls
            spec = M.METRICS[metric]
            link = r[_C_LINK] if isinstance(r[_C_LINK], str) and r[_C_LINK].startswith("http") else None
            cur, pri = _num(r[_C_CUR]), _num(r[_C_PRIOR])
            # Growth rows leave PRIOR as a 0 placeholder (no growth-of-growth).
            if metric == "gdpi_growth" and pri == 0:
                pri = None
            for p, v, rank in ((period, cur, 0), (prior, pri, 1)):
                if v is None:
                    continue
                v = v * spec.scale
                offer(Fact(p, "quarter", metric, dim, entity, v, name, link), (rank, newer))
                if is_q4:
                    annual = p.split("_")[0]
                    offer(Fact(annual, "annual", metric, dim, entity, v, name, link), (1 + rank, newer))

    hist_path = historical_path or paths.HISTORICAL_TRENDS_WORKBOOK
    if os.path.isfile(str(hist_path)):
        from competitor_analysis.reporting import historical
        hname = os.path.basename(str(hist_path))
        sources.append({"file": hname, "period": None, "kind": "historical"})
        for title, table in historical.load_historical_tables(hist_path).items():
            metric = M.HISTORICAL_TABLES.get(title.strip().lower())
            if metric is None:
                continue
            for entity, series in table.items():
                for year, v in series.items():
                    v = _num(v)
                    if v is None:
                        continue
                    try:
                        period = make_period(year)
                        parse_period(period)
                    except (ValueError, TypeError):
                        continue
                    offer(Fact(period, "annual", metric, None, entity, v, hname), (0, 0))

    facts = [f for _, f in candidates.values()]
    facts.sort(key=lambda f: (period_sort_key(f.period), f.metric, f.entity, f.dimension or ""))
    return FactStore(facts, sources)


# ---------------------------------------------------------------------------
# Process-wide cached instance, rebuilt whenever its source files change
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cached = {"fingerprint": None, "store": None}


def _fingerprint():
    files = sorted(glob.glob(os.path.join(str(paths.OUTPUT_DIR), "Data_Engine_*.xlsx")))
    files.append(str(paths.HISTORICAL_TRENDS_WORKBOOK))
    return tuple((f, os.path.getmtime(f)) for f in files if os.path.exists(f))


def get_store():
    """The shared FactStore. Cheap to call per request: it only rebuilds when
    a Data Engine is added/re-written or the history workbook changes, so a
    pipeline run or a manual workbook upload is picked up automatically."""
    fp = _fingerprint()
    with _lock:
        if _cached["fingerprint"] != fp:
            _cached["store"] = build_store()
            _cached["fingerprint"] = fp
        return _cached["store"]
