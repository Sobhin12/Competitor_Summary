"""
Statistical screen: cheap, deterministic hints about where to look.

This is NOT insight - it only points the investigating AI at the places where
the data is unusual, so it spends its effort on real candidates rather than
inventing patterns. Three kinds of hint, all over year-on-year movement so
that year-to-date seasonality drops out:

  outlier       a company's YoY move is large against its OWN history of moves
                (z-score; needs MIN_HISTORY earlier points)
  peer_outlier  a company sits far from the peer median on its YoY move
  trend_shift   the latest YoY move reverses the direction of the previous few
                and is larger than their spread (a change of trajectory)
  rank_change   a company's rank among the established insurers moved

Read-only: it queries the FactStore and returns plain dicts.
"""
import statistics

from competitor_analysis.analysis import formula as L
from competitor_analysis.analysis import metrics as M
from competitor_analysis.analysis import store as S
from competitor_analysis.analysis import tools as T

MIN_HISTORY = 4
FLOW_UNITS = {"cr", "count", "lakh", "inr"}


def _move(unit, cur, prior):
    """YoY movement on a comparable scale: relative for amounts, absolute for ratios."""
    if cur is None or prior is None:
        return None
    if unit in FLOW_UNITS:
        return None if abs(prior) < 1e-9 else cur / prior - 1
    return cur - prior


def _history_moves(store, metric, entity, unit, upto):
    """YoY moves at every earlier quarter that has both points, oldest first."""
    out = []
    for p in store.periods("quarter", metric):
        if S.period_sort_key(p) >= S.period_sort_key(upto):
            continue
        m = _move(unit, store.value(metric, entity, p), store.value(metric, entity, S.prior_year_period(p)))
        if m is not None:
            out.append(m)
    return out


def _z(x, hist):
    if len(hist) < MIN_HISTORY:
        return None
    sd = statistics.pstdev(hist)
    return None if sd < 1e-9 else (x - statistics.mean(hist)) / sd


def screen(store, period, max_hints=60):
    py = S.prior_year_period(period)
    rows, examined = [], 0
    for metric in store.metric_ids():
        spec = M.METRICS[metric]
        if spec.dimension or spec.unit in ("raw",):
            continue
        moves = {}
        for e in M.COMPANY_KEYS:
            if e in L.EARLY_STAGE:
                continue
            f, pf = store.fact(metric, e, period), store.fact(metric, e, py)
            if f is None or pf is None:
                continue
            m = _move(spec.unit, f.value, pf.value)
            if m is None or T.suspect_jump(f.value, pf.value, spec.unit):
                continue
            moves[e] = (m, f, pf)
        if len(moves) < L.MIN_PEERS:
            continue
        examined += 1
        med = statistics.median(v[0] for v in moves.values())
        mad = statistics.median(abs(v[0] - med) for v in moves.values()) or 1e-9
        order = sorted(moves, key=lambda e: moves[e][0], reverse=(spec.higher_is_better is not False))
        for e, (m, f, pf) in moves.items():
            hist = _history_moves(store, metric, e, spec.unit, period)
            z = _z(m, hist)
            base = {"metric": metric, "label": spec.label, "entity": e, "unit": spec.unit,
                    "value": f.value, "display": T.fmt(f.value, spec.unit), "prior_display": T.fmt(pf.value, spec.unit),
                    "move": m, "z": z, "ids": [f.id, pf.id]}
            weight = spec.weight * (0.5 if spec.higher_is_better is None else 1.0)
            if z is not None and abs(z) >= 2:
                rows.append({**base, "kind": "outlier", "score": abs(z) * weight})
            peer = abs(m - med) / (1.4826 * mad)
            if peer >= 3:
                rows.append({**base, "kind": "peer_outlier", "score": peer * 0.6 * weight})
            if len(hist) >= 4:
                recent, last = hist[-3:], m
                sd = statistics.pstdev(recent) or 1e-9
                if (last > 0) != (statistics.mean(recent) > 0) and abs(last - statistics.mean(recent)) > 1.5 * sd:
                    rows.append({**base, "kind": "trend_shift", "recent_moves": recent,
                                 "score": 2.5 * weight})
        # rank change vs a year ago among the same companies
        if spec.higher_is_better is not None:
            prior_order = sorted(moves, key=lambda e: moves[e][2].value, reverse=spec.higher_is_better)
            cur_order = sorted(moves, key=lambda e: moves[e][1].value, reverse=spec.higher_is_better)
            for e in moves:
                shift = prior_order.index(e) - cur_order.index(e)
                if abs(shift) >= 2:
                    f, pf = moves[e][1], moves[e][2]
                    rows.append({"kind": "rank_change", "metric": metric, "label": spec.label, "entity": e,
                                 "unit": spec.unit, "value": f.value, "display": T.fmt(f.value, spec.unit),
                                 "prior_display": T.fmt(pf.value, spec.unit), "move": moves[e][0], "z": None,
                                 "rank_shift": shift, "ids": [f.id, pf.id], "score": abs(shift) * spec.weight})
    rows.sort(key=lambda r: -r["score"])
    return {"examined_metrics": examined, "total_flagged": len(rows), "hints": rows[:max_hints]}
