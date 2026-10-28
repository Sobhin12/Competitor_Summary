"""Human-readable provenance for derived Data Engine cells.

A derived row's value is still computed in Python; a Trace records, for the
Formula / Inputs columns beside it, what the arithmetic was and which input
values (with their source form and unit) fed it - so a wrong number can be
told apart as a bad extraction (check the input against the filing) or bad
arithmetic (recompute from the row)."""
from dataclasses import dataclass, field

FORMULA_COL = 11
INPUTS_COL = 12


@dataclass
class Inp:
    """One input value: label, value as it was used, source form, unit."""
    label: str
    value: object
    source: str = ""
    unit: str = ""


def fmt_num(v):
    """Indian-grouped number (4,29,500.00); 'n/a' for a missing input."""
    if v is None:
        return "n/a"
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    neg = v < 0
    v = abs(v)
    if v != int(v) or isinstance(v, float):
        dec = 4 if v < 10 else 2
        whole, frac = f"{v:.{dec}f}".split(".")
        frac = frac.rstrip("0").ljust(2, "0")
    else:
        whole, frac = str(int(v)), ""
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    out = whole + ("." + frac if frac else "")
    return "-" + out if neg else out


def _render_inputs(inps):
    parts = []
    for i in inps:
        src = f" [{i.source}]" if i.source else ""
        unit = f" {i.unit}" if i.unit else ""
        parts.append(f"{i.label}{src} {fmt_num(i.value)}{unit}")
    return "; ".join(parts)


@dataclass
class Trace:
    formula: str
    cur: list = field(default_factory=list)
    prior: list = field(default_factory=list)
    note: str | None = None

    def render(self):
        """(formula_text, inputs_text) for columns K and L."""
        segs = []
        if self.cur:
            segs.append("CUR: " + _render_inputs(self.cur))
        if self.prior:
            segs.append("PRIOR: " + _render_inputs(self.prior))
        if self.note:
            segs.append("NOTE: " + self.note)
        return self.formula, " | ".join(segs)


def make(formula, names, cur_vals, prior_vals=None, sources=None, units=None, note=None):
    """Convenience: names=[label,...]; cur_vals/prior_vals parallel lists;
    sources/units are dicts or parallel lists (default empty)."""
    def pick(x, i):
        if x is None:
            return ""
        return x[i] if isinstance(x, (list, tuple)) else x.get(names[i], "")
    cur = [Inp(n, cur_vals[i], pick(sources, i), pick(units, i)) for i, n in enumerate(names)]
    prior = ([Inp(n, prior_vals[i], pick(sources, i), pick(units, i)) for i, n in enumerate(names)]
             if prior_vals is not None else [])
    return Trace(formula, cur, prior, note)
