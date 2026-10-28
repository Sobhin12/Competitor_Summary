"""
Formula engine: evaluates a hypothesis' formula for every peer insurer.

The investigating AI writes its own formulas (nothing is stored as a template);
this module is the ONLY place an insight figure is computed, so nothing an LLM
says ever becomes a number on the page. Each result carries the fact ids it was
built from. Used by briefing.py for test_formula, claim verification and forward
calls, and by screen.py.

Formula language (a safe subset of Python expressions, parsed with `ast`):
  cur(m[, dim])    value of metric m this period       prior(m[, dim])  comparison value
  chg(m[, dim])    cur - prior                         gr(m[, dim])     cur / prior - 1
  share(m)         company's share of the 7-company total, cur   pshare(m)  same, comparison period
  + - * / ( ) abs min max, numbers, comparisons, and/or/not.
Expressions that also see the peer set (`fires`, `impact`) get: home, best, worst,
median, rank, n, nflag, annualise, and home_cur(m).

Bases (the `basis` field):
  ytd      cur = year-to-date as filed, comparison = same YTD one year earlier.
           Used for everything except sequential analysis.
  quarter  cur = the single quarter (YTD less the previous quarter's YTD),
           comparison = the previous quarter. Quarter-to-quarter analysis only.
           Flow metrics (crore amounts) only - a ratio or balance cannot be
           differenced, so build it from flows instead (claims / earned premium).
A spec never mixes the two. A company whose inputs are missing for the period
is left out rather than estimated.
"""
import ast
import statistics

from competitor_analysis.analysis import metrics as M
from competitor_analysis.analysis import store as S

HOME = "NBHI"
# Insurers still in their first years: tiny bases swing every ratio (KNOWN_ISSUES.md §2).
EARLY_STAGE = {"NARAYANA", "GALAXY"}
MIN_PEERS = 4
# Balances at a point in time (not flows) - these cannot be differenced into a quarter.
STOCK_METRICS = {"capital", "net_worth", "aum", "aum_policyholders", "aum_shareholders", "investment_portfolio",
                 "solvency_ratio"}


class FormulaError(ValueError):
    """A malformed formula spec."""


class _Skip(Exception):
    """An input this company does not have for the period."""


_FUNCS = {"cur", "prior", "chg", "gr", "share", "pshare", "home_cur", "abs", "min", "max"}
_PEER_VARS = {"home", "best", "worst", "median", "rank", "n", "nflag", "annualise"}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.Constant,
                  ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Not, ast.And,
                  ast.Or, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


# ---------------------------------------------------------------------------
# Validation (also the gate any AI-proposed hypothesis has to pass)
# ---------------------------------------------------------------------------

def _parse(expr, field):
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"{field}: not a valid formula ({e.msg})")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError(f"{field}: {type(node).__name__} is not allowed in a formula")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
                raise FormulaError(f"{field}: only {', '.join(sorted(_FUNCS))} may be called")
    return tree


def _metric_args(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.func.id not in {"abs", "min", "max"}:
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise FormulaError("metric names are passed as strings, e.g. cur('pbt')")
            yield node.func.id, node.args[0].value


def validate_spec(spec):
    """Raise FormulaError unless the spec is well formed and uses real metrics."""
    for key in ("id", "title", "theme", "basis", "formula", "unit", "so_what"):
        if not spec.get(key):
            raise FormulaError(f"spec {spec.get('id')!r}: missing {key!r}")
    if spec["basis"] not in ("ytd", "quarter"):
        raise FormulaError(f"spec {spec['id']!r}: basis must be 'ytd' or 'quarter'")
    for field in ("formula", "require", "flag", "show_prior", "fires", "impact"):
        if not spec.get(field):
            continue
        tree = _parse(spec[field], field)
        for fn, metric in _metric_args(tree):
            if metric not in M.METRICS:
                raise FormulaError(f"spec {spec['id']!r}: unknown metric {metric!r}")
            if spec["basis"] == "quarter" and fn != "home_cur" and (
                    M.METRICS[metric].unit != "cr" or metric in STOCK_METRICS):
                raise FormulaError(f"spec {spec['id']!r}: {metric!r} is not a flow, so it has no single-quarter "
                                f"figure - build a quarterly ratio from flows instead")
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _FUNCS}
        peer = _PEER_VARS if field in ("fires", "impact") else set()
        if names - peer:
            raise FormulaError(f"{field}: unknown name(s) {sorted(names - peer)}")
    return spec


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _eval(node, funcs, names):
    if isinstance(node, ast.Expression):
        return _eval(node.body, funcs, names)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise _Skip(node.id)
        return names[node.id]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, funcs, names)
        if isinstance(node.op, ast.Not):
            return not v
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, funcs, names), _eval(node.right, funcs, names)
        if isinstance(node.op, ast.Div):
            if abs(b) < 1e-9:
                raise _Skip("division by ~0")
            return a / b
        return a + b if isinstance(node.op, ast.Add) else a - b if isinstance(node.op, ast.Sub) else a * b
    if isinstance(node, ast.BoolOp):
        vals = (_eval(v, funcs, names) for v in node.values)
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Compare):
        left, out = _eval(node.left, funcs, names), True
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, funcs, names)
            out = out and {ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right,
                           ast.GtE: left >= right, ast.Eq: left == right, ast.NotEq: left != right}[type(op)]
            left = right
        return out
    if isinstance(node, ast.Call):
        args = [_eval(a, funcs, names) for a in node.args]
        return funcs[node.func.id](*args)
    raise FormulaError(f"cannot evaluate {type(node).__name__}")


def _months(period):
    _, q = S.parse_period(period)
    return 3 * (q or 4)


def _previous_quarter(period):
    start, q = S.parse_period(period)
    if q > 1:
        return f"FY{start:02d}-{(start + 1) % 100:02d}_Q{q - 1}"
    return f"FY{(start - 1) % 100:02d}-{start % 100:02d}_Q4"


class _Inputs:
    """Reads facts for one evaluation under its basis, remembering which fact
    ids each company's figure came from."""

    def __init__(self, store, period, basis):
        self.s, self.p, self.basis = store, period, basis
        self.py = S.prior_year_period(period)
        self.pq = _previous_quarter(period)
        self.ids = {}

    def _ytd(self, metric, entity, period, dim):
        f = self.s.fact(metric, entity, period, dim)
        if f is None:
            raise _Skip(f"{metric}@{period}")
        self.ids.setdefault(entity, set()).add(f.id)
        return f.value

    def _quarter(self, metric, entity, period, dim):
        """Single-quarter flow: this YTD less the previous quarter's YTD."""
        if S.parse_period(period)[1] == 1:
            return self._ytd(metric, entity, period, dim)
        return self._ytd(metric, entity, period, dim) - self._ytd(metric, entity, _previous_quarter(period), dim)

    def value(self, metric, entity, comparison, dim=None):
        if self.basis == "ytd":
            return self._ytd(metric, entity, self.py if comparison else self.p, dim)
        return self._quarter(metric, entity, self.pq if comparison else self.p, dim)

    def total(self, metric, comparison):
        vals = []
        for c in M.COMPANY_KEYS:
            try:
                vals.append(self.value(metric, c, comparison))
            except _Skip:
                continue
        if not vals:
            raise _Skip(f"total {metric}")
        return sum(vals)


def _company_funcs(inp, entity):
    def cur(m, dim=None):
        return inp.value(m, entity, False, dim)

    def prior(m, dim=None):
        return inp.value(m, entity, True, dim)

    def gr(m, dim=None):
        base = prior(m, dim)
        if base <= 1e-9:
            raise _Skip(f"no base for {m}")
        return cur(m, dim) / base - 1

    return {"cur": cur, "prior": prior, "chg": lambda m, d=None: cur(m, d) - prior(m, d), "gr": gr,
            "share": lambda m: cur(m) / inp.total(m, False), "pshare": lambda m: prior(m) / inp.total(m, True),
            "abs": abs, "min": min, "max": max}


def _fmt(v, unit):
    if v is None:
        return "n/a"
    return {"pct": f"{v * 100:.1f}%", "pp": f"{v * 100:+.1f} pp", "x": f"{v:.2f}x", "rs100": f"{'-' if v < 0 else '+'}₹{abs(v):.1f}",
            "cr": f"₹{v:,.0f} cr"}.get(unit, f"{v:,.2f}")


class _Fmt(dict):
    def __missing__(self, key):
        return "n/a"


def evaluate(store, spec, period):
    """Evaluate one spec for a period. Returns the structured result, or
    None when fewer than MIN_PEERS established insurers have the inputs."""
    validate_spec(spec)
    basis, unit = spec["basis"], spec["unit"]
    inp = _Inputs(store, period, basis)
    formula = _parse(spec["formula"], "formula")
    require = _parse(spec["require"], "require") if spec.get("require") else None
    flag = _parse(spec["flag"], "flag") if spec.get("flag") else None
    rows = []
    for e in M.COMPANY_KEYS:
        if e in EARLY_STAGE:
            continue
        funcs = _company_funcs(inp, e)
        try:
            if require is not None and not _eval(require, funcs, {}):
                continue
            value = _eval(formula, funcs, {})
            flagged = bool(_eval(flag, funcs, {})) if flag is not None else False
            prev = None
            if spec.get("show_prior"):
                prev = _eval(_parse(spec["show_prior"], "show_prior"), funcs, {})
        except _Skip:
            continue
        rows.append({"entity": e, "name": M.COMPANY_NAMES.get(e, e), "value": value, "display": _fmt(value, unit),
                     "prior": prev, "prior_display": _fmt(prev, unit) if prev is not None else None,
                     "flagged": flagged, "ids": sorted(inp.ids.get(e, ()))})
    if len(rows) < MIN_PEERS or not any(r["entity"] == HOME for r in rows):
        return None

    hib = spec.get("higher_is_better")
    ordered = sorted(rows, key=lambda r: r["value"], reverse=(hib is not False))
    for i, r in enumerate(ordered):
        r["rank"] = i + 1
    values = [r["value"] for r in rows]
    home = next(r for r in rows if r["entity"] == HOME)
    best, worst = ordered[0], ordered[-1]
    annualise = 12 / _months(period) if basis == "ytd" else 4
    names = {"home": home["value"], "best": best["value"], "worst": worst["value"],
             "median": statistics.median(values), "rank": home["rank"], "n": len(rows),
             "nflag": sum(r["flagged"] for r in rows), "annualise": annualise}
    peer_funcs = _company_funcs(inp, HOME)
    peer_funcs["home_cur"] = peer_funcs["cur"]

    def rule(field, default=None):
        if not spec.get(field):
            return default
        try:
            return _eval(_parse(spec[field], field), peer_funcs, names)
        except _Skip:
            return default

    fired = bool(rule("fires", True))
    impact = rule("impact")
    flagged_names = [r["name"] for r in rows if r["flagged"]]
    text = _Fmt(home=home["display"], best=best["display"], best_name=best["name"], worst=worst["display"],
                worst_name=worst["name"], median=_fmt(names["median"], unit), rank=str(home["rank"]),
                n=str(len(rows)), flagged=", ".join(flagged_names) or "none",
                gap=_fmt(abs(home["value"] - best["value"]), "pp" if unit in ("pct", "pp") else unit),
                impact=_fmt(abs(impact), "cr") if impact is not None else "n/a")
    for r in rows:
        text[r["entity"].lower()] = r["display"]
    return {
        "id": spec["id"], "title": spec["title"], "theme": spec["theme"], "basis": basis, "unit": unit,
        "higher_is_better": hib, "fired": fired, "formula": spec["formula"],
        "headline": spec["so_what"].format_map(text), "caveats": list(spec.get("caveats", [])),
        "impact_cr": impact, "impact_display": text["impact"] if impact is not None else None,
        "home": {k: home[k] for k in ("entity", "name", "display", "rank")}, "n": len(rows),
        "best": {"entity": best["entity"], "name": best["name"], "display": best["display"]},
        "flagged": flagged_names, "rows": ordered, "median": text["median"],
        "worst": {"entity": worst["entity"], "name": worst["name"], "display": worst["display"]},
        "evidence_ids": sorted({i for r in rows for i in r["ids"]}),
        "priority": spec.get("priority", 1.0),
    }


def position_map(store, period):
    """Growth (x) vs health combined ratio (y), with last year's combined
    ratio so the page can draw each player's movement."""
    inp = _Inputs(store, period, "ytd")
    out = []
    for e in M.COMPANY_KEYS:
        if e in EARLY_STAGE:
            continue
        f = _company_funcs(inp, e)
        try:
            x, y, y0 = f["gr"]("gwp"), f["cur"]("health_combined_ratio"), f["prior"]("health_combined_ratio")
        except _Skip:
            continue
        out.append({"entity": e, "name": M.COMPANY_NAMES.get(e, e), "growth": x, "combined_ratio": y,
                    "prior_combined_ratio": y0})
    return out
