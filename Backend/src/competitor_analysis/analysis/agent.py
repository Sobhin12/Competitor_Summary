"""
Chat agent: an LLM that answers questions about the Data Engine by calling
the deterministic tools in tools.py, and can emit charts/tables for the UI.

Ground rules enforced here, not just requested in the prompt:
- every number the model writes is checked against this request's tool
  results (verify.py); unverified ones are reported to the UI;
- chart values must be values some tool returned;
- arithmetic goes through the `calculate` tool, whose operands must
  themselves come from tool results.

run() is synchronous and reports progress through an `emit(event, data)`
callback; the API wraps it into a Server-Sent Events stream.
"""
import ast
import json
import operator
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from competitor_analysis.analysis import deepdive, insights, llm, verify
from competitor_analysis.analysis import metrics as M
from competitor_analysis.analysis import store as S
from competitor_analysis.analysis import tools as T

MAX_STEPS = 8
MAX_HISTORY = 12


# ---------------------------------------------------------------------------
# Output blocks
# ---------------------------------------------------------------------------

class Series(BaseModel):
    name: str
    values: list[float | None]


class ChartSpec(BaseModel):
    type: Literal["bar", "grouped_bar", "stacked_bar", "line", "doughnut", "waterfall"]
    title: str
    unit: Literal["cr", "pct", "pp", "x", "count", "lakh", "inr", "raw"] = "raw"
    categories: list[str] = Field(min_length=1, max_length=40)
    series: list[Series] = Field(min_length=1, max_length=12)
    note: str | None = None

    @model_validator(mode="after")
    def _lengths(self):
        for s in self.series:
            if len(s.values) != len(self.categories):
                raise ValueError(f"series {s.name!r} has {len(s.values)} values for {len(self.categories)} categories")
        return self


class TableSpec(BaseModel):
    title: str
    columns: list[str] = Field(min_length=1, max_length=12)
    rows: list[list[str | float | int | None]] = Field(min_length=1, max_length=60)


_CHART_SCHEMA = {"type": "object", "properties": {
    "type": {"type": "string", "enum": ["bar", "grouped_bar", "stacked_bar", "line", "doughnut", "waterfall"]},
    "title": {"type": "string"},
    "unit": {"type": "string", "enum": ["cr", "pct", "pp", "x", "count", "lakh", "inr", "raw"],
             "description": "pct values are fractions exactly as tools return them (0.253 = 25.3%); "
                            "pp values are percentage points (1.2)."},
    "categories": {"type": "array", "items": {"type": "string"},
                   "description": "x-axis labels (companies, periods, segments...)"},
    "series": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "values": {"type": "array", "items": {"type": ["number", "null"]}}},
        "required": ["name", "values"]}},
    "note": {"type": "string"}}, "required": ["type", "title", "unit", "categories", "series"]}

_TABLE_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
    "rows": {"type": "array", "items": {"type": "array", "items": {"type": ["string", "number", "null"]}}}},
    "required": ["title", "columns", "rows"]}


# ---------------------------------------------------------------------------
# Extra tools that need the agent's state
# ---------------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node, operands):
    if isinstance(node, ast.Expression):
        return _eval(node.body, operands)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        operands.append(float(node.value))
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left, operands), _eval(node.right, operands))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand, operands))
    raise T.ToolError("calculate only supports numbers and + - * / ** ( ).")


def _compact_highlights(store, period=None, entity=None, n=8):
    rep = insights.build(store, period, max_per_theme=99 if entity else 4)
    ents = T._entities([entity]) if entity else None
    rows = [i for i in rep["insights"] if ents is None or i["entity"] in ents][:n]
    return {"period": rep["period"], "insights": [
        {"theme": i["theme"], "entity": i["entity"], "headline": i["headline"], "drivers": i["drivers"],
         "implication": i["implication"], "context": i["context"], "caveats": i["caveats"],
         "evidence": i["evidence"]} for i in rows],
        "data_checks": [c["message"] for c in rep["data_checks"]
                        if ents is None or set(c["entities"]) & set(ents)][:5]}


def _compact_profile(store, company, period=None):
    d = deepdive.build(store, company, period)
    slim = lambda r: {k: r.get(k) for k in ("label", "display", "prior_display", "change", "rank", "of",
                                            "peer_median_display", "sahi_display", "ids")}
    return {"company": d["company"], "name": d["name"], "period": d["period"], "early_stage": d["early_stage"],
            "kpis": [slim(r) for r in d["kpis"]], "strengths": [slim(r) for r in d["strengths"]],
            "weaknesses": [slim(r) for r in d["weaknesses"]],
            "insights": [i["headline"] for i in d["insights"]], "data_checks": [c["message"] for c in d["data_checks"]]}


def tool_specs():
    specs = [{"name": n, "description": d, "parameters": schema} for n, (_, schema, d) in T.TOOLS.items()]
    specs += [
        {"name": "highlights", "description": "Rule-based insights for a period: what moved, the drivers that "
                                              "explain it, implications and caveats. Optional entity filter.",
         "parameters": {"type": "object", "properties": {"period": {"type": "string"}, "entity": {"type": "string"},
                                                          "n": {"type": "integer"}}}},
        {"name": "company_profile", "description": "One company's KPIs, ranks, strengths, weaknesses and insight "
                                                   "headlines for a period.",
         "parameters": {"type": "object", "properties": {"company": {"type": "string"}, "period": {"type": "string"}},
                        "required": ["company"]}},
        {"name": "calculate", "description": "Evaluate arithmetic (+ - * / ** parentheses) on numbers taken from "
                                             "earlier tool results, e.g. a ratio or difference. Never do maths "
                                             "in your head.",
         "parameters": {"type": "object", "properties": {"expression": {"type": "string"}},
                        "required": ["expression"]}},
        {"name": "render_chart", "description": "Show a chart to the user. Values must be copied exactly from "
                                                "tool results. Prefer one clear chart over several.",
         "parameters": _CHART_SCHEMA},
        {"name": "render_table", "description": "Show a compact table to the user (copy values from tool results; "
                                                "display strings are fine).",
         "parameters": _TABLE_SCHEMA},
    ]
    return specs


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

STATE_MAX_ENTITIES = 5
STATE_MAX_METRICS = 6


def _recent(old, new, limit):
    """`new` items first (most recent focus), then older ones not repeated."""
    out = []
    for x in [*new, *old]:
        if x and x not in out:
            out.append(x)
    return out[:limit]


def update_state(state, calls, period=None):
    """Fold this turn's tool calls into the session's working focus: which
    companies and metrics the user is looking at and for which period. Rule
    based on purpose - it can't hallucinate a focus the tools never touched."""
    ents, mets, per = [], [], None
    for c in calls:
        a = c.get("args") or {}
        for e in [*(a.get("entities") or []), a.get("entity"), a.get("company")]:
            key = M.entity_key(e) if isinstance(e, str) else None
            if key and key not in ents:
                ents.append(key)
        if isinstance(a.get("metric"), str) and a["metric"] in M.METRICS and a["metric"] not in mets:
            mets.append(a["metric"])
        if isinstance(a.get("period"), str) and a["period"]:
            per = a["period"]
    state = dict(state or {})
    state["entities"] = _recent(state.get("entities", []), ents, STATE_MAX_ENTITIES)
    state["metrics"] = _recent(state.get("metrics", []), mets, STATE_MAX_METRICS)
    state["period"] = period or per or state.get("period")
    return state


def _state_block(state):
    if not state or not (state.get("entities") or state.get("metrics") or state.get("period")):
        return ""
    lines = []
    if state.get("entities"):
        lines.append("- Companies in focus (most recent first): " + ", ".join(state["entities"]))
    if state.get("metrics"):
        lines.append("- Metrics in focus (most recent first): " + ", ".join(state["metrics"]))
    if state.get("period"):
        lines.append(f"- Period in focus: {state['period']}")
    return ("\n\nConversation context (what earlier turns were about). Use it to resolve follow-ups such as "
            "\"their\", \"that metric\" or \"the same for last year\"; the user's latest message wins if it "
            "names something else. Figures from earlier turns are NOT available - call tools again.\n"
            + "\n".join(lines))


def system_prompt(store, period=None, state=None):
    period = period or store.latest_quarter()
    by_section = {}
    for mid in store.metric_ids():
        m = M.METRICS[mid]
        if m.section == "Other":
            continue
        by_section.setdefault(m.section, []).append(
            f"{mid} ({m.unit}{', split by ' + m.dimension.lower() if m.dimension else ''})")
    catalog = "\n".join(f"- {s}: {', '.join(v)}" for s, v in by_section.items())
    checks = [c["message"] for c in insights.data_checks(store, period)][:4] if period else []
    companies = ", ".join(f"{k} = {v}" for k, v in M.COMPANY_NAMES.items())
    return f"""You are a competitive-intelligence analyst for the Indian standalone health insurance (SAHI) market.
You answer questions using ONLY the data available through your tools, which read IRDAI public disclosures
compiled into the Data Engine.

Companies: {companies}. Aggregates: SAHI (the 7 together), Industry (all general + health insurers).
Narayana and Galaxy are early-stage insurers - their ratios and growth swing wildly on a tiny base; say so
when they appear in a comparison.

Periods: quarterly ids look like 'FY26-27_Q1' and are YEAR-TO-DATE as filed (Q3 = Apr-Dec); annual ids look like
'FY25-26' (full year). The latest quarter is {period} ({S.period_label(period) if period else 'none'}).
"Last year"/"YoY" means the same period one year earlier. Default to the latest quarter when none is given.

Metric ids by section (unit in brackets; pct values are fractions, 0.253 = 25.3%):
{catalog}

Rules:
1. Every number you state must come from a tool result in this conversation. Never recall, estimate or
   compute figures yourself - use `calculate` for any arithmetic (differences, ratios, averages).
2. Be neutral: no company is "us". Compare on the facts.
3. Explain, don't just list: say what changed, what drove it (use `highlights`/`breakdown` drivers) and why it
   matters. Mark interpretation as such ("this suggests...").
4. When a chart would help (comparisons across companies, trends, mixes), call `render_chart` once with values
   copied exactly from tool results. Use `render_table` for multi-metric comparisons.
5. Format numbers the way tools display them (₹ Cr, %, pp, x). Name the period you are quoting.
6. Keep answers tight: a one-line answer first, then supporting points as short bullets. No preamble.
7. If the data can't answer the question, say what is missing instead of guessing.

Known data-quality warnings for {period}:
{chr(10).join('- ' + c for c in checks) if checks else '- none'}
Mention a warning when your answer relies on the affected company's figures.{_state_block(state)}"""


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

class Agent:
    def __init__(self, store=None, client=None, emit=None):
        self.store = store or S.get_store()
        self.client = client or llm.get_client()
        self.emit = emit or (lambda event, data: None)
        self.results = []          # every tool result this request, for verification
        self.blocks = []           # charts/tables emitted
        self.user_numbers = []

    # -- tool execution ---------------------------------------------------------
    def _run_tool(self, name, args):
        if name == "highlights":
            return _compact_highlights(self.store, **args)
        if name == "company_profile":
            return _compact_profile(self.store, **args)
        if name == "calculate":
            expr = str(args.get("expression", ""))
            try:
                tree = ast.parse(expr, mode="eval")
            except SyntaxError:
                raise T.ToolError(f"Can't parse {expr!r}.")
            operands = []
            result = _eval(tree, operands)
            check = verify.verify(" ".join(f"{o:.10g}" for o in operands if abs(o) > 10 or o != int(o)),
                                  self.results, self.user_numbers)
            if check["unverified"]:
                raise T.ToolError(f"Operands {check['unverified']} don't appear in any tool result - "
                                  f"use exact values returned by tools.")
            return {"expression": expr, "result": result}
        if name == "render_chart":
            spec = ChartSpec.model_validate(args)
            values = [v for s in spec.series for v in s.values if v is not None]
            bad = [v for v in values if not self._known(v, spec.unit)]
            if bad:
                raise T.ToolError(f"Chart values {bad[:5]} are not in any tool result. Copy values exactly as "
                                  f"tools returned them (pct as fractions).")
            block = {"type": "chart", "spec": spec.model_dump()}
            self.blocks.append(block)
            self.emit("chart", block["spec"])
            return {"shown": True}
        if name == "render_table":
            spec = TableSpec.model_validate(args)
            block = {"type": "table", "spec": spec.model_dump()}
            self.blocks.append(block)
            self.emit("table", block["spec"])
            return {"shown": True}
        return T.call(self.store, name, args)

    def _known(self, value, unit):
        tol = 0.0006 if unit == "pct" else 0.06 if unit in ("pp", "x") else max(0.51, abs(value) * 1e-4)
        for res in self.results:
            for v, _ in verify._walk(res):
                for c in (v, v * 100, abs(v), abs(v) * 100):
                    if abs(c - value) <= tol:
                        return True
        return False

    # -- main loop --------------------------------------------------------------
    def run(self, history, period=None, state=None):
        history = [m for m in history if m.get("role") in ("user", "assistant") and m.get("content")][-MAX_HISTORY:]
        if not history or history[-1]["role"] != "user":
            raise ValueError("The last message must be from the user.")
        self.user_numbers = [n["value"] for n in verify.verify(history[-1]["content"], [])["numbers"]]
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        system = system_prompt(self.store, T._period(self.store, period) if period else None, state)
        tools = tool_specs()
        answer = []
        calls = []

        for _ in range(MAX_STEPS):
            turn = self.client.complete(system, messages, tools,
                                        on_text=lambda t: (answer.append(t), self.emit("text", t)))
            messages.append({"role": "assistant", "content": turn.text, "raw": turn.raw,
                             "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in turn.tool_calls]})
            if not turn.tool_calls:
                break
            if turn.text:
                # Separate interim narration ("Let me check...") from what follows.
                answer.append("\n\n")
                self.emit("text", "\n\n")
            for call in turn.tool_calls:
                calls.append({"name": call.name, "args": call.args})
                self.emit("tool_status", {"name": call.name, "args": call.args})
                try:
                    result = self._run_tool(call.name, call.args)
                    if call.name not in ("render_chart", "render_table"):
                        self.results.append(result)
                    content = json.dumps(result, default=str)
                except (T.ToolError, ValidationError, ValueError) as e:
                    content = json.dumps({"error": str(e)})
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
        else:
            note = "\n\n_(Stopped after the maximum number of analysis steps.)_"
            answer.append(note)
            self.emit("text", note)

        text = "".join(answer).strip()
        check = verify.verify(text, self.results, self.user_numbers)
        cited = []
        for n in check["numbers"]:
            for fid in n["ids"]:
                if fid not in cited:
                    cited.append(fid)
        citations = [self._citation(fid) for fid in cited[:30]]
        citations = [c for c in citations if c]
        self.emit("verification", {"checked": len(check["numbers"]), "unverified": check["unverified"]})
        self.emit("citations", citations)
        return {"text": text, "blocks": self.blocks, "verification": check, "citations": citations,
                "tools": calls, "state": update_state(state, calls, period),
                "provider": self.client.name, "model": self.client.model}

    def _citation(self, fid):
        f = self.store.get(fid)
        if f is None:
            return None
        spec = M.METRICS[f.metric]
        return {"id": fid, "label": spec.label + (f" - {f.dimension}" if f.dimension else ""),
                "entity": f.entity, "period": f.period, "period_label": S.period_label(f.period),
                "display": T.fmt(f.value, spec.unit), "source": f.source, "source_link": f.source_link}
