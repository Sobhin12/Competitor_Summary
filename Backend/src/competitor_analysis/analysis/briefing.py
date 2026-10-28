"""
The executive briefing: an AI analyst investigates the quarter, code audits
every claim, and the AI writes up only what survived. Runs once per new
quarter (cached by the source workbooks' fingerprint).

    screen (code)       statistical hints about where the data is unusual
    investigate (AI)    tests hypotheses with read-only tools and free formulas
    verify (code)       every submitted finding is re-computed from the Data
                        Engine; its claim must hold, its figures must be real,
                        implausible values are quarantined
    challenge (AI)      a second pass argues the other side; weak findings drop
    rank (code)         rupee materiality x relevance x confidence x novelty
    narrate (AI)        storylines across the survivors; every number checked

Nothing here writes to the Data Engine. The AI's tool surface is an explicit
read-only whitelist, the FactStore opens workbooks read-only, and run() takes a
checksum of every Data Engine file before and after - a difference raises and
discards the briefing. Output goes only under artifacts/cache/briefing/.
"""
import glob
import hashlib
import json
import math
import os
import re
import threading
import time

from competitor_analysis import paths
from competitor_analysis.analysis import insights, formula as L, llm, screen as Sc, verify
from competitor_analysis.analysis import metrics as M
from competitor_analysis.analysis import store as S
from competitor_analysis.analysis import tools as T

CACHE_DIR = paths.CACHE_DIR / "briefing"
MAX_STEPS = 45
MAX_FINDINGS = 9
KEEP_TOP = 6
MAX_WATCH = 4

READ_TOOLS = set(T.TOOLS)            # list_metrics, compare, trend, ... - all read the FactStore
AI_TOOLS = READ_TOOLS | {"test_formula", "submit_finding", "dismiss", "submit_watch"}
CONF_W = {"low": 0.6, "medium": 0.85, "high": 1.0}


# ---------------------------------------------------------------------------
# Data Engine write guard
# ---------------------------------------------------------------------------

def engine_snapshot():
    """{file: sha1} of every Data Engine workbook and the history workbook."""
    files = glob.glob(os.path.join(str(paths.OUTPUT_DIR), "Data_Engine_*.xlsx"))
    files.append(str(paths.HISTORICAL_TRENDS_WORKBOOK))
    out = {}
    for f in sorted(files):
        if os.path.isfile(f):
            with open(f, "rb") as fh:
                out[f] = hashlib.sha1(fh.read()).hexdigest()
    return out


class DataEngineModified(RuntimeError):
    pass


def _fingerprint(store):
    parts = [(s["file"], os.path.getmtime(os.path.join(str(paths.OUTPUT_DIR), s["file"]))
              if os.path.isfile(os.path.join(str(paths.OUTPUT_DIR), s["file"])) else 0) for s in store.sources]
    return hashlib.md5(json.dumps(sorted(parts)).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

INVESTIGATOR = """You are the lead analyst briefing the CEO and CFO of Niva Bupa (NBHI) on India's standalone health
insurers (SAHI). Your job is to find what is HARD TO SEE by reading the quarterly report: relationships across several
metrics, who is doing what and why it matters for NBHI. Not a recap of what moved.

Data: the Data Engine (IRDAI disclosures) for {period_label}. Figures are YEAR-TO-DATE as filed; comparison is the same
YTD a year earlier. Peers compared: NBHI, STAR, CARE, CIGNA, ABHI (Narayana and Galaxy are too young - excluded).
Metric ids by section (unit; pct values are fractions):
{catalog}

How to work:
1. Think like a CFO's strategy analyst. Angles worth testing (not a checklist - pick what the data supports): whether
   growth pays for itself (profit per rupee of new premium); cost growing faster than premium; earnings that depend on
   investment income or reinsurance rather than underwriting; capital headroom against growth and losses; who is gaining
   or ceding share and how it is funded; channel or segment mix shifts that reveal strategy; a company whose
   trajectory has just changed; where NBHI stands against the best peer, priced in rupees.
   Retail-sales productivity, agent counts and similar operational metrics matter only if they explain one of these.
2. Start from the statistical hints you are given (they only say where the data is unusual). Form hypotheses about what is
   going on commercially - growth bought with losses, cost outrunning premium, reliance on reinsurance, earnings that
   depend on investment income, capital running thin, share moving between players, strategy shifts in channel or segment
   mix, a change in a company's trajectory.
3. Test each hypothesis with test_formula, which computes any formula for every peer. Formula language: cur(m) prior(m)
   chg(m) gr(m) share(m) pshare(m) with metric ids as strings, e.g. chg('pbt') / chg('gwp') * 100, plus + - * / abs min
   max and comparisons. Split metrics take a dimension: cur('channel_mix','Banca'). basis 'ytd' is the default;
   'quarter' (single quarter, flows only) only for quarter-to-quarter questions.
4. When a hypothesis holds, call submit_finding. Each claim is re-computed by code from the Data Engine: its `check` (an
   expression over home, best, worst, median, rank, n, nflag) must be true, and every number in your summary and
   so_what must appear in the results of your claims. If it is rejected, read why and fix it.
5. When a hypothesis fails or is a data/base artefact, call dismiss with the reason. Showing what you ruled out matters.
6. Give a rupee value wherever you can (impact: an expression in crore a year, e.g. (home - best) *
   home_cur('health_earned_premium') * annualise). Executives react to rupees, not percentage points.
7. Call submit_watch for up to {max_watch} forward-looking calls code can grade next quarter (e.g. "CARE solvency below
   1.5x") - a formula plus a check that will be true if the call comes true.
8. Aim for {max_findings} strong findings that connect to each other. Stop when you have them; do not pad.

A finding must say something a single slide of the report does not. Combine metrics (a ratio of changes, a gap between
growth rates, a split of profit into sources) or measure a change against peers. A lone current value ("NBHI's solvency
is 2.25x", "ceding is near the median") is a restatement, and code rejects it. pct metrics are fractions displayed as
percentages: never multiply by 100. In `check`, `impact` and `flag` use only the allowed names and functions; no other
calls (no round, len, str). Examples of checks: rank >= n - 1, home - best > 0.1, home < median, nflag > 0.

Write from NBHI's side: what it means for us. Interpretation is welcome but phrase it as such ("suggests", "likely").
Never state a number that a tool did not return. Never compute in your head."""

CHALLENGER = """You are a sceptical reviewer of one finding in a competitive briefing for a health insurer's CEO.
You get the finding, the figures behind it and known data warnings. Argue the other side: is this a base effect, an
extraction/data problem, a timing or seasonality artefact, a one-off, a different company's problem mislabelled as a
trend, or a simpler explanation? Reply with ONLY JSON: {"verdict": "keep" | "soften" | "drop", "objection": "<one or two
sentences>", "caveat": "<short caveat to show readers, or empty>"}. Use "drop" only when the finding is likely
misleading; "soften" when it holds but needs a caveat. Use no numbers that are not in the input."""

NARRATOR = """You write the executive briefing for the CEO and CFO of Niva Bupa (NBHI) from verified findings.
Reply with ONLY JSON:
{"headline": "<one sentence: the single most important thing this quarter>",
 "storylines": [{"title": "<short>", "narrative": "<3-5 sentences connecting the findings: cause, effect, what it means
   for NBHI, what to do or watch>", "finding_ids": ["f1", ...]}],
 "new_this_quarter": ["<what is new or changed versus last quarter's briefing, one line each>"]}
Rules: 2-4 storylines that connect findings across companies and metrics (e.g. a share loss, growth bought with losses,
capital pressure and the opening it creates for NBHI), not one per finding. Use ONLY numbers that appear in the input,
written exactly as given there; never compute new numbers. Interpretation must be phrased as such ("suggests", "likely").
Mention the largest caveat where it matters. Every finding id must appear in exactly one storyline. If last quarter's calls
are graded in the input, mention which held and which did not."""


class _Throttled:
    """Wraps the LLM client so a long tool loop stays inside a per-minute quota
    (Gemini's free tier allows 15 requests a minute) and rides out a 429."""

    def __init__(self, client):
        self.client, self.name, self.model = client, client.name, client.model
        default = 4.3 if client.name == "gemini" else 0.0
        self.gap = float(os.getenv("BRIEFING_MIN_INTERVAL", default))
        self._last = 0.0

    def complete(self, system, messages, tools, on_text=None):
        for attempt in range(4):
            wait = self.gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                return self.client.complete(system, messages, tools, on_text)
            except Exception as e:  # noqa: BLE001 - quota and transient network/server errors are retried
                text = f"{type(e).__name__} {e}"
                transient = any(k in text for k in ("429", "RESOURCE_EXHAUSTED", "RemoteProtocolError", "Timeout",
                                                    "ConnectError", "ReadError", "503", "500", "UNAVAILABLE"))
                if not transient or attempt == 3:
                    raise
                m = re.search(r"retry in ([\d.]+)s", text)
                time.sleep(min(float(m.group(1)) + 1 if m else 8 * (attempt + 1), 70))


def _catalog(store):
    by_section = {}
    for mid in store.metric_ids():
        m = M.METRICS[mid]
        if m.section != "Other":
            by_section.setdefault(m.section, []).append(
                f"{mid} ({m.unit}{', split by ' + m.dimension.lower() if m.dimension else ''})")
    return "\n".join(f"- {s}: {', '.join(v)}" for s, v in by_section.items())


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool schemas for the investigator
# ---------------------------------------------------------------------------

_CLAIM = {"type": "object", "properties": {
    "formula": {"type": "string"}, "basis": {"type": "string", "enum": ["ytd", "quarter"]},
    "unit": {"type": "string", "enum": ["pct", "pp", "x", "rs100", "cr"]},
    "higher_is_better": {"type": ["boolean", "null"]},
    "require": {"type": "string", "description": "optional guard, e.g. chg('gwp') > 0"},
    "flag": {"type": "string", "description": "optional per-company condition marking who the story is about"},
    "check": {"type": "string", "description": "must be TRUE for the claim to hold; uses home, best, worst, median, "
                                               "rank, n, nflag"},
    "impact": {"type": "string", "description": "optional rupee value to NBHI in crore a year"}},
    "required": ["formula", "unit", "check"]}

_SPEC_PROPS = {"formula": {"type": "string"}, "basis": {"type": "string", "enum": ["ytd", "quarter"]},
               "unit": {"type": "string", "enum": ["pct", "pp", "x", "rs100", "cr"]},
               "higher_is_better": {"type": ["boolean", "null"]}, "require": {"type": "string"},
               "flag": {"type": "string"}}


def _tool_specs():
    specs = [{"name": n, "description": d, "parameters": schema} for n, (_, schema, d) in T.TOOLS.items()]
    specs += [
        {"name": "test_formula", "description": "Compute a formula for every peer (NBHI, STAR, CARE, CIGNA, ABHI) and "
                                                "see values, ranks, best/worst/median. Use to test a hypothesis.",
         "parameters": {"type": "object", "properties": _SPEC_PROPS, "required": ["formula", "unit"]}},
        {"name": "submit_finding", "description": "Submit a finding. Code re-computes every claim; rejects it if a "
                                                  "claim does not hold or a number is not backed.",
         "parameters": {"type": "object", "properties": {
             "title": {"type": "string"}, "storyline": {"type": "string", "description": "short theme label"},
             "direction": {"type": "string", "enum": ["good", "bad", "mixed"], "description": "for NBHI"},
             "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
             "entities": {"type": "array", "items": {"type": "string"}},
             "summary": {"type": "string", "description": "2-3 sentences with the evidence numbers"},
             "so_what": {"type": "string", "description": "what it means for NBHI"},
             "claims": {"type": "array", "items": _CLAIM, "minItems": 1, "maxItems": 4}},
             "required": ["title", "direction", "confidence", "entities", "summary", "so_what", "claims"]}},
        {"name": "dismiss", "description": "Record a hypothesis you tested and ruled out, with the reason.",
         "parameters": {"type": "object", "properties": {"hypothesis": {"type": "string"}, "reason": {"type": "string"}},
                        "required": ["hypothesis", "reason"]}},
        {"name": "submit_watch", "description": "A forward-looking call code can grade next quarter. `check` is true "
                                                 "when the call comes true.",
         "parameters": {"type": "object", "properties": {
             "statement": {"type": "string"}, **_SPEC_PROPS, "check": {"type": "string"}},
             "required": ["statement", "formula", "unit", "check"]}},
    ]
    return specs


# ---------------------------------------------------------------------------
# The investigation
# ---------------------------------------------------------------------------

def _spec(args, **extra):
    spec = {"id": "adhoc", "title": "adhoc", "theme": "adhoc", "so_what": "{home}",
            "basis": args.get("basis") or "ytd", "unit": args.get("unit") or "pct",
            "formula": args.get("formula"), "higher_is_better": args.get("higher_is_better")}
    for k in ("require", "flag"):
        if args.get(k):
            spec[k] = args[k]
    spec.update(extra)
    return spec


def _slim(res):
    """An evaluate() result trimmed to what the AI and the briefing need."""
    return {"formula": res["formula"], "basis": res["basis"], "unit": res["unit"],
            "home": res["home"], "best": res["best"], "n": res["n"], "flagged": res["flagged"],
            "worst": res["worst"], "median": res["median"],
            "impact_cr": res["impact_cr"], "impact_display": res["impact_display"],
            "rows": [{"entity": r["entity"], "name": r["name"], "display": r["display"], "value": r["value"],
                      "prior_display": r.get("prior_display"), "flagged": r["flagged"], "rank": r["rank"],
                      "ids": r["ids"]} for r in res["rows"]],
            "evidence_ids": res["evidence_ids"]}


class Investigation:
    def __init__(self, store, period, client, emit, prior):
        self.store, self.period, self.client, self.emit, self.prior = store, period, client, emit, prior
        self.results = []               # every tool result, for number verification
        self.findings, self.dismissed, self.watch, self.rejected = [], [], [], []
        self.tested = 0
        self.warnings = insights.data_checks(store, period)

    # -- tools --------------------------------------------------------------
    def _run(self, spec):
        try:
            res = L.evaluate(self.store, spec, self.period)
        except L.FormulaError as e:
            raise T.ToolError(str(e))
        if res is None:
            raise T.ToolError("Too few established insurers have the inputs for this formula and period.")
        return res

    def test_formula(self, args):
        res = self._run(_spec(args))
        self.tested += 1
        out = _slim(res)
        self.results.append(out)
        self.emit("test", f"Tested {args['formula']} - NBHI ranks {out['home']['rank']} of {out['n']}")
        return out

    def _anomaly(self, res):
        for r in res["rows"]:
            lim = {"pct": 10, "x": 50, "pp": 10, "rs100": 1000, "cr": 1e6}.get(res["unit"])
            if lim and abs(r["value"]) > lim:
                return f"{r['name']} value {r['display']} is outside the plausible range - held back as a likely data issue"
        return None

    def _caveats(self, claim_results):
        used = {m for r in claim_results for m in re.findall(r"'([a-z_0-9]+)'", r["formula"])}
        out = []
        for c in self.warnings:
            ents = set(c["entities"]) & {row["entity"] for r in claim_results for row in r["rows"]}
            if ents and (set(c["metrics"]) & used or c["kind"] == "duplicate_values"):
                out.append(c["message"])
        return out[:2]

    def submit_finding(self, a):
        if len(self.findings) >= MAX_FINDINGS:
            raise T.ToolError("Enough findings - finish with a short closing note.")
        claims, results = [], []
        for i, c in enumerate(a["claims"]):
            res = self._run(_spec(c, fires=c["check"], **({"impact": c["impact"]} if c.get("impact") else {})))
            if not res["fired"]:
                raise T.ToolError(f"Claim {i + 1} does not hold: check `{c['check']}` is false. NBHI {res['home']['display']} "
                                  f"(rank {res['home']['rank']} of {res['n']}), best {res['best']['display']}, worst "
                                  f"{res['rows'][-1]['display']}.")
            bad = self._anomaly(res)
            if bad:
                self.rejected.append({"title": a["title"], "reason": bad})
                raise T.ToolError(bad)
            claims.append({**_slim(res), "check": c["check"]})
            results.append(res)
        used = {m for r in results for m in re.findall(r"'([a-z_0-9]+)'", r["formula"])}
        relational = any(re.search(r"(chg|gr|share|pshare|prior)\(", r["formula"]) for r in results)
        if len(used) < 2 and not relational:
            raise T.ToolError("This restates one metric's current value. Combine two or more metrics, or measure a "
                              "change (chg/gr/share), so it says something the report does not.")
        text = f"{a['summary']} {a['so_what']}"
        chk = verify.verify(text, [self.results[-8:], claims])
        if chk["unverified"]:
            raise T.ToolError(f"These numbers are not in your claims' results: {chk['unverified']}. Use only figures "
                              f"the tools returned, written as displayed, or test a formula that produces them.")
        impact = next((abs(r["impact_cr"]) for r in results if r["impact_cr"] is not None), None)
        metrics = sorted({m for r in results for m in re.findall(r"'([a-z_0-9]+)'", r["formula"])})
        fid = f"f{len(self.findings) + 1}"
        caveats = self._caveats(results) + [x for r in results for x in r["caveats"]]
        self.findings.append({
            "id": fid, "title": a["title"], "storyline": a.get("storyline") or "", "direction": a["direction"],
            "confidence": a["confidence"], "entities": a["entities"], "summary": a["summary"], "so_what": a["so_what"],
            "impact_cr": impact, "impact_display": L._fmt(impact, "cr") if impact is not None else None,
            "claims": claims, "metrics": metrics, "caveats": caveats[:3], "numbers_checked": len(chk["numbers"]),
            "challenge": None})
        self.emit("finding", f"Verified finding: {a['title']}")
        return {"accepted": fid, "numbers_checked": len(chk["numbers"])}

    def dismiss(self, a):
        chk = verify.verify(a["reason"], self.results)
        if chk["unverified"]:
            raise T.ToolError(f"Numbers in the reason are not in any result: {chk['unverified']}.")
        self.dismissed.append({"hypothesis": a["hypothesis"], "reason": a["reason"], "source": "analyst"})
        self.emit("dismiss", f"Ruled out: {a['hypothesis']}")
        return {"recorded": True}

    def submit_watch(self, a):
        if len(self.watch) >= MAX_WATCH:
            raise T.ToolError("Enough forward calls.")
        spec = _spec(a, fires=a["check"])
        res = self._run(spec)
        self.watch.append({"statement": a["statement"], "spec": {k: v for k, v in spec.items() if k not in ("id", "title", "theme", "so_what")},
                           "baseline_true": bool(res["fired"]), "baseline_home": res["home"]["display"]})
        self.emit("watch", f"Forward call: {a['statement']}")
        return {"recorded": True, "currently_true": bool(res["fired"])}

    def dispatch(self, name, args):
        if name not in AI_TOOLS:
            raise T.ToolError(f"Unknown tool {name!r}.")
        if name == "test_formula":
            return self.test_formula(args)
        if name == "submit_finding":
            return self.submit_finding(args)
        if name == "dismiss":
            return self.dismiss(args)
        if name == "submit_watch":
            return self.submit_watch(args)
        out = T.call(self.store, name, args)
        self.results.append(out)
        return out

    # -- loop -----------------------------------------------------------------
    def run(self, hints, graded):
        system = INVESTIGATOR.format(period_label=S.period_label(self.period), catalog=_catalog(self.store),
                                     max_watch=MAX_WATCH, max_findings=KEEP_TOP + 2)
        brief = {"statistical_hints": [{k: h.get(k) for k in ("kind", "label", "entity", "display", "prior_display",
                                                              "z", "rank_shift", "recent_moves")} for h in hints[:40]],
                 "data_warnings": [c["message"] for c in self.warnings][:6]}
        if self.prior:
            brief["last_quarter_findings"] = [f["title"] for f in self.prior.get("findings", [])]
            brief["last_quarter_calls_graded"] = graded
        messages = [{"role": "user", "content": "Begin the investigation. Context:\n" + json.dumps(brief, default=str)}]
        tools = _tool_specs()
        for _ in range(MAX_STEPS):
            turn = self.client.complete(system, messages, tools)
            messages.append({"role": "assistant", "content": turn.text, "raw": turn.raw,
                             "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in turn.tool_calls]})
            if not turn.tool_calls:
                break
            for call in turn.tool_calls:
                try:
                    content = json.dumps(self.dispatch(call.name, call.args), default=str)
                except (T.ToolError, L.FormulaError, KeyError, TypeError, ValueError) as e:
                    content = json.dumps({"error": str(e)})
                    if call.name == "submit_finding":
                        self.rejected.append({"title": (call.args or {}).get("title", "?"), "reason": str(e)})
                        self.emit("reject", f"Code rejected a finding: {str(e)[:140]}")
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
            if len(self.findings) >= MAX_FINDINGS:
                break


# ---------------------------------------------------------------------------
# Prior briefing, grading, ranking
# ---------------------------------------------------------------------------

def _all_cached():
    out = {}
    for f in glob.glob(str(CACHE_DIR / "FY*.json")):
        period = os.path.basename(f).split("_")[0] + ("_" + os.path.basename(f).split("_")[1]
                                                      if os.path.basename(f).split("_")[1].startswith("Q") else "")
        out.setdefault(period, []).append(f)
    return out


def load_prior(period):
    cached = _all_cached()
    earlier = [p for p in cached if S.period_sort_key(p) < S.period_sort_key(period)]
    if not earlier:
        return None
    best = max(earlier, key=S.period_sort_key)
    path = max(cached[best], key=os.path.getmtime)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def grade_calls(store, prior, period):
    graded = []
    for call in (prior or {}).get("calls", []):
        try:
            res = L.evaluate(store, {"id": "call", "title": "call", "theme": "call", "so_what": "{home}", **call["spec"]}, period)
        except L.FormulaError:
            res = None
        outcome = "cannot be assessed" if res is None else "came true" if res["fired"] else "has not happened"
        graded.append({"statement": call["statement"], "outcome": outcome, "was_true_before": call["baseline_true"],
                       "now": res["home"]["display"] if res else None})
    return graded


def _rank(findings, prior):
    seen = {(frozenset(f["entities"]), frozenset(f["metrics"])): f for f in (prior or {}).get("findings", [])}
    for f in findings:
        mat = 1 + math.log10(1 + (f["impact_cr"] or 0))
        rel = 1.0 if "NBHI" in f["entities"] else 0.7
        old = seen.get((frozenset(f["entities"]), frozenset(f["metrics"])))
        if old is None:
            nov = 1.0
        else:
            grew = (f["impact_cr"] or 0) > 1.2 * (old.get("impact_cr") or 0)
            nov = 0.9 if grew else 0.55
        f["novelty"] = nov
        f["score"] = round(mat * rel * CONF_W.get(f["confidence"], 0.6) * nov, 3)
    findings.sort(key=lambda f: -f["score"])


def _challenge(inv, findings):
    kept, ruled = [], []
    for f in findings:
        payload = {"finding": {k: f[k] for k in ("title", "summary", "so_what", "direction", "caveats")},
                   "figures": [{"formula": c["formula"], "rows": [(r["name"], r["display"]) for r in c["rows"]]}
                               for c in f["claims"]],
                   "data_warnings": [c["message"] for c in inv.warnings][:5]}
        try:
            turn = inv.client.complete(CHALLENGER, [{"role": "user", "content": json.dumps(payload, default=str)}], [])
            verdict = _json(turn.text) or {}
        except Exception:  # noqa: BLE001 - a failed review must not sink the briefing
            verdict = {}
        v = verdict.get("verdict", "keep")
        f["challenge"] = {"verdict": v, "objection": verdict.get("objection", "")}
        if verdict.get("caveat"):
            f["caveats"] = [verdict["caveat"], *f["caveats"]][:3]
        if v == "drop":
            ruled.append({"hypothesis": f["title"], "reason": verdict.get("objection") or "Judged misleading on review.",
                          "source": "challenge"})
            inv.emit("challenge", f"Challenge dropped: {f['title']}")
        else:
            kept.append(f)
            inv.emit("challenge", f"Survived challenge: {f['title']}")
    return kept, ruled


def _narrate(inv, findings, ruled, graded, prior):
    evidence = {"findings": [{"id": f["id"], "title": f["title"], "direction": f["direction"], "summary": f["summary"],
                              "so_what": f["so_what"], "impact": f["impact_display"], "caveats": f["caveats"]}
                             for f in findings],
                "ruled_out": [r["hypothesis"] for r in ruled][:6],
                "last_quarter_calls_graded": graded,
                "last_quarter_headlines": [f["title"] for f in (prior or {}).get("findings", [])]}
    source = [evidence, *[c for f in findings for c in f["claims"]]]
    content = json.dumps(evidence, default=str)
    out, unver = None, []
    for attempt in range(2):
        turn = inv.client.complete(NARRATOR, [{"role": "user", "content": content}], [])
        out = _json(turn.text)
        if not out:
            continue
        text = " ".join([out.get("headline", ""), *out.get("new_this_quarter", []),
                         *[s.get("narrative", "") for s in out.get("storylines", [])]])
        unver = verify.verify(text, source)["unverified"]
        if not unver:
            break
        content = json.dumps({**evidence, "fix": f"These numbers are not in the input: {unver}. Rewrite without them."})
    return out, unver


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build(store, period=None, client=None, emit=None):
    emit = emit or (lambda kind, text: None)
    period = T._period(store, period)
    client = _Throttled(client or llm.get_client())
    before = engine_snapshot()
    prior = load_prior(period)
    graded = grade_calls(store, prior, period)
    sc = Sc.screen(store, period)
    emit("screen", f"Screened {sc['examined_metrics']} metrics; {sc['total_flagged']} unusual patterns found")

    inv = Investigation(store, period, client, emit, prior)
    try:
        inv.run(sc["hints"], graded)
    except Exception as e:  # noqa: BLE001 - keep what was already verified if the model call finally fails
        if not inv.findings:
            raise
        emit("reject", f"The investigation stopped early ({type(e).__name__}); using the {len(inv.findings)} findings verified so far")
    findings = inv.findings
    _rank(findings, prior)
    top, rest = findings[:KEEP_TOP], findings[KEEP_TOP:]
    emit("challenge", f"Challenging the top {len(top)} findings")
    kept, challenged = _challenge(inv, top)
    ruled = inv.dismissed + challenged + [{"hypothesis": r["title"], "reason": r["reason"], "source": "code"}
                                          for r in inv.rejected if "outside the plausible" in r["reason"]]
    emit("narrate", "Writing the briefing")
    story, unver = _narrate(inv, kept, ruled, graded, prior) if kept else (None, [])
    story = story or {}
    placed = {i for s in story.get("storylines", []) for i in s.get("finding_ids", [])}
    storylines = [{"title": s.get("title", ""), "narrative": s.get("narrative", ""),
                   "findings": [i for i in s.get("finding_ids", []) if any(f["id"] == i for f in kept)]}
                  for s in story.get("storylines", [])]
    left = [f["id"] for f in kept if f["id"] not in placed]
    if left:
        storylines.append({"title": "Also notable", "narrative": "", "findings": left})
    headline = story.get("headline") or (kept[0]["so_what"] if kept else
                                         "No material change this quarter stood out against the data's own history.")

    after = engine_snapshot()
    if before != after:
        raise DataEngineModified("A Data Engine file changed during the briefing run - discarded.")
    checked = sum(f["numbers_checked"] for f in kept)
    return {
        "period": period, "period_label": S.period_label(period), "generated_at": int(time.time()),
        "provider": client.name, "model": client.model, "headline": headline, "storylines": storylines,
        "new_this_quarter": story.get("new_this_quarter", []), "findings": kept,
        "also_verified": [{"id": f["id"], "title": f["title"], "summary": f["summary"]} for f in rest],
        "ruled_out": ruled, "calls": inv.watch, "graded_calls": graded,
        "trend_shifts": [h for h in sc["hints"] if h["kind"] == "trend_shift"][:6],
        "position_map": L.position_map(store, period),
        "stats": {"examined_metrics": sc["examined_metrics"], "patterns_flagged": sc["total_flagged"],
                  "hypotheses_tested": inv.tested, "findings_submitted": len(inv.findings) + len(inv.rejected),
                  "rejected_by_code": len(inv.rejected), "challenged_out": len(challenged),
                  "findings_kept": len(kept), "numbers_checked": checked},
        "unverified": unver, "data_warnings": [c["message"] for c in inv.warnings][:5],
        "engine_unchanged": True,
    }


# ---------------------------------------------------------------------------
# Background job + cache, so a new quarter's briefing builds itself on first view
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_jobs = {}


def _path(store, period):
    return CACHE_DIR / f"{period}_{_fingerprint(store)}.json"


def cached(store, period):
    p = _path(store, period)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def status(store, period=None, refresh=False, start=True):
    """{'status': ready|running|error|unavailable, ...}; starts a build when
    there is none cached and an LLM is available."""
    period = T._period(store, period)
    if not refresh:
        hit = cached(store, period)
        if hit:
            return {"status": "ready", "briefing": hit}
    with _lock:
        job = _jobs.get(period)
        if job and job["state"] == "running":
            return {"status": "running", "events": job["events"][-60:]}
        if job and job["state"] == "error" and not refresh:
            return {"status": "error", "error": job["error"], "events": job["events"][-60:]}
        if not start:
            return {"status": "none"}
        try:
            client = llm.get_client()
        except llm.LLMUnavailable as e:
            return {"status": "unavailable", "reason": str(e)}
        job = _jobs[period] = {"state": "running", "events": [], "error": None}

    def emit(kind, text):
        job["events"].append({"t": int(time.time()), "kind": kind, "text": text})

    def work():
        try:
            out = build(store, period, client, emit)
            os.makedirs(CACHE_DIR, exist_ok=True)
            _path(store, period).write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001 - surfaced to the UI, never raised into the request
            job["state"], job["error"] = "error", f"{type(e).__name__}: {str(e)[:300]}"

    threading.Thread(target=work, daemon=True).start()
    return {"status": "running", "events": []}
