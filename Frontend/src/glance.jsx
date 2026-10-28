import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "./api.js";
import { Badge, Button, Card, Spinner } from "./ui.jsx";
import { COMPANY_COLORS } from "./charts.jsx";
import {
  CompanyDot, ErrorCard, LoadingCard, PeriodSelect, companyName, useAnalysisMeta,
} from "./analysisShared.jsx";

// ---------------------------------------------------------------------------
// At a glance: the executive landing page. Ranked second-order insights
// (lenses) written from NBHI's side, not a wall of numbers. Every figure is
// computed by the backend lens engine; this view only lays them out.
// ---------------------------------------------------------------------------

const BASIS = { ytd: "Year to date", quarter: "Single quarter" };

function PositionMap({ points }) {
  if (!points?.length) return null;
  const W = 560, H = 300, P = { l: 52, r: 20, t: 16, b: 40 };
  const xs = points.map((p) => p.growth * 100);
  const ys = points.flatMap((p) => [p.combined_ratio * 100, p.prior_combined_ratio * 100]);
  const [x0, x1] = [Math.min(...xs, 0) - 3, Math.max(...xs) + 3];
  const [y0, y1] = [Math.min(...ys, 100) - 3, Math.max(...ys) + 3];
  const sx = (v) => P.l + ((v - x0) / (x1 - x0)) * (W - P.l - P.r);
  const sy = (v) => H - P.b - ((v - y0) / (y1 - y0)) * (H - P.t - P.b);
  return (
    <Card className="p-5">
      <div className="text-sm font-semibold text-white">Position map</div>
      <p className="text-xs text-slate-500 mb-2">
        Growth vs health combined ratio. Arrows run from last year's combined ratio to this year's; below the dashed line is underwriting profit.
      </p>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Growth against combined ratio">
        <defs>
          <marker id="arr" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="#94a3b8" />
          </marker>
        </defs>
        <line x1={P.l} x2={W - P.r} y1={sy(100)} y2={sy(100)} stroke="#475569" strokeDasharray="4 4" />
        <text x={W - P.r} y={sy(100) - 4} textAnchor="end" fontSize="10" fill="#64748b">100% combined</text>
        <text x={W / 2} y={H - 6} textAnchor="middle" fontSize="11" fill="#94a3b8">Premium growth (%)</text>
        <text transform={`translate(12 ${H / 2}) rotate(-90)`} textAnchor="middle" fontSize="11" fill="#94a3b8">Health combined ratio (%)</text>
        {[x0, x1].map((v) => <text key={v} x={sx(v)} y={H - P.b + 14} textAnchor="middle" fontSize="10" fill="#64748b">{v.toFixed(0)}</text>)}
        {[y0, y1].map((v) => <text key={v} x={P.l - 6} y={sy(v) + 3} textAnchor="end" fontSize="10" fill="#64748b">{v.toFixed(0)}</text>)}
        {points.map((p) => {
          const c = COMPANY_COLORS[p.entity] || "#94a3b8";
          const x = sx(p.growth * 100);
          return (
            <g key={p.entity}>
              <line x1={x} x2={x} y1={sy(p.prior_combined_ratio * 100)} y2={sy(p.combined_ratio * 100)}
                    stroke={c} strokeOpacity="0.6" markerEnd="url(#arr)" />
              <circle cx={x} cy={sy(p.combined_ratio * 100)} r={p.entity === "NBHI" ? 7 : 5} fill={c} />
              <text x={x + 9} y={sy(p.combined_ratio * 100) + 3} fontSize="11" fill="#e2e8f0">{p.name}</text>
            </g>
          );
        })}
      </svg>
    </Card>
  );
}

function PeerBars({ lens }) {
  const vals = lens.rows.map((r) => r.value);
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const span = hi - lo || 1;
  return (
    <div className="space-y-1.5 mt-3">
      {lens.rows.map((r) => {
        const left = ((Math.min(0, r.value) - lo) / span) * 100;
        const width = Math.max((Math.abs(r.value) / span) * 100, 1.5);
        return (
          <div key={r.entity} className="flex items-center gap-2 text-xs">
            <span className={`w-28 shrink-0 truncate ${r.entity === "NBHI" ? "text-white font-semibold" : "text-slate-400"}`}>
              <CompanyDot company={r.entity} className="mr-1.5" />{r.name}
            </span>
            <div className="relative flex-1 h-3 rounded bg-slate-900">
              <div className="absolute h-3 rounded" style={{
                left: `${left}%`, width: `${width}%`,
                background: COMPANY_COLORS[r.entity] || "#64748b", opacity: r.flagged ? 1 : 0.55,
              }} />
            </div>
            <span className="w-16 text-right tabular-nums text-slate-300">{r.display}</span>
            {r.flagged && <span className="text-[10px] text-amber-400">flag</span>}
          </div>
        );
      })}
    </div>
  );
}

function LensCard({ lens, rank, onAsk }) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="p-5">
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 shrink-0 rounded-full bg-brand-500/15 border border-brand-500/30 text-brand-300 text-xs font-semibold flex items-center justify-center">
          {rank}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] uppercase tracking-wide text-slate-500">{lens.theme} · {BASIS[lens.basis]}</div>
          <div className="text-sm font-semibold text-white mt-0.5">{lens.title}</div>
          <p className="text-sm text-slate-300 mt-2 leading-relaxed">{lens.headline}</p>
          <div className="flex flex-wrap items-center gap-2 mt-3">
            <Badge tone="slate">NBHI ranks {lens.home.rank} of {lens.n}</Badge>
            {lens.impact_display && <Badge tone="amber">{lens.impact_display} a year at stake</Badge>}
          </div>
        </div>
      </div>
      <PeerBars lens={lens} />
      <div className="flex gap-4 mt-3 text-xs">
        <button className="text-slate-400 hover:text-white" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"} how this is calculated
        </button>
        {onAsk && (
          <button className="text-brand-300 hover:text-white"
                  onClick={() => onAsk(`Explain "${lens.title}" for NBHI and what we should do about it.`)}>
            Ask more →
          </button>
        )}
      </div>
      {open && (
        <div className="mt-2 rounded-lg bg-slate-900/70 p-3 text-xs text-slate-400 space-y-1">
          <div><span className="text-slate-500">Formula</span> <code className="text-slate-300">{lens.formula}</code></div>
          <div><span className="text-slate-500">Source figures</span> {lens.evidence_ids.length} Data Engine facts</div>
          {lens.caveats.map((c) => <div key={c}>Caveat: {c}</div>)}
        </div>
      )}
    </Card>
  );
}


// ---------------------------------------------------------------------------
// AI briefing. An AI analyst investigates the quarter, code re-computes every
// claim from the Data Engine, a second pass challenges the findings, and the
// AI writes up only what survived. The page shows that work, not just results.
// ---------------------------------------------------------------------------

const DIRECTION = {
  bad: { tone: "rose", label: "Against NBHI" },
  good: { tone: "emerald", label: "For NBHI" },
  mixed: { tone: "amber", label: "Mixed for NBHI" },
};
const KIND_DOT = {
  screen: "bg-sky-400", test: "bg-slate-500", finding: "bg-emerald-400", reject: "bg-rose-400",
  dismiss: "bg-amber-400", challenge: "bg-violet-400", narrate: "bg-brand-400", watch: "bg-cyan-400",
};
const OUTCOME = {
  "came true": "text-emerald-400", "has not happened": "text-slate-400", "cannot be assessed": "text-slate-500",
};

function InvestigationLog({ events }) {
  const end = useRef(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "nearest" }); }, [events.length]);
  return (
    <Card className="p-5">
      <div className="flex items-center gap-2 mb-3 text-sm font-semibold text-white">
        <Spinner /> The analyst is investigating this quarter…
      </div>
      <div className="max-h-72 overflow-y-auto space-y-1.5 pr-1">
        {events.length === 0 && <div className="text-xs text-slate-500">Screening the data for unusual patterns…</div>}
        {events.map((e, i) => (
          <div key={i} className="flex items-start gap-2 text-xs text-slate-300">
            <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${KIND_DOT[e.kind] || "bg-slate-500"}`} />
            <span>{e.text}</span>
          </div>
        ))}
        <div ref={end} />
      </div>
      <p className="text-[11px] text-slate-500 mt-3">
        Every claim is re-computed from the Data Engine before it can appear here. This runs once per quarter.
      </p>
    </Card>
  );
}

function Stats({ s }) {
  const items = [
    [s.examined_metrics, "metrics screened"], [s.patterns_flagged, "unusual patterns"],
    [s.hypotheses_tested, "hypotheses tested"], [s.rejected_by_code + s.challenged_out, "rejected or ruled out"],
    [s.findings_kept, "findings kept"], [s.numbers_checked, "figures re-computed"],
  ];
  return (
    <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
      {items.map(([v, l]) => (
        <div key={l} className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2">
          <div className="text-lg font-semibold text-white tabular-nums">{v}</div>
          <div className="text-[10px] uppercase tracking-wide text-slate-500">{l}</div>
        </div>
      ))}
    </div>
  );
}

function FindingCard({ f, onAsk }) {
  const [open, setOpen] = useState(false);
  const dir = DIRECTION[f.direction] || DIRECTION.mixed;
  const claim = f.claims[0];
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-center gap-2 mb-1.5">
        <Badge tone={dir.tone}>{dir.label}</Badge>
        {f.impact_display && <Badge tone="amber">{f.impact_display} a year at stake</Badge>}
        <Badge tone="slate">{f.confidence} confidence</Badge>
        <span className="ml-auto text-[11px] text-emerald-400">✓ {f.numbers_checked} figures re-computed</span>
      </div>
      <div className="text-sm font-semibold text-white">{f.title}</div>
      <p className="text-sm text-slate-300 mt-2 leading-relaxed">{f.summary}</p>
      <p className="text-sm text-white mt-2 leading-relaxed">
        <span className="text-brand-300 font-medium">For NBHI: </span>{f.so_what}
      </p>
      {claim && <PeerBars lens={claim} />}
      {f.caveats.map((c) => <p key={c} className="text-[11px] text-amber-300/80 mt-2">Caveat: {c}</p>)}
      <div className="flex gap-4 mt-3 text-xs">
        <button className="text-slate-400 hover:text-white" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"} how it was found
        </button>
        {onAsk && (
          <button className="text-brand-300 hover:text-white"
                  onClick={() => onAsk(`Dig deeper into: ${f.title}. What should NBHI do?`)}>
            Ask more →
          </button>
        )}
      </div>
      {open && (
        <div className="mt-2 rounded-lg bg-slate-900/70 p-3 text-xs text-slate-400 space-y-2">
          {f.claims.map((c, i) => (
            <div key={i}>
              <div><span className="text-slate-500">Calculation</span> <code className="text-slate-300">{c.formula}</code></div>
              <div><span className="text-slate-500">Claim tested by code</span> <code className="text-slate-300">{c.check}</code> → true</div>
              <div><span className="text-slate-500">Source</span> {c.evidence_ids.length} Data Engine facts</div>
            </div>
          ))}
          {f.challenge && (
            <div>
              <span className="text-slate-500">Reviewer</span> {f.challenge.verdict}
              {f.challenge.objection ? ` - ${f.challenge.objection}` : ""}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function Briefing({ b, onAsk }) {
  const byId = Object.fromEntries(b.findings.map((f) => [f.id, f]));
  return (
    <div className="space-y-5">
      <Card className="p-6 border-brand-500/30 bg-gradient-to-br from-brand-500/10 to-transparent">
        <div className="text-[11px] uppercase tracking-wide text-brand-300">The one thing this quarter</div>
        <p className="text-lg text-white font-medium mt-1.5 leading-snug">{b.headline}</p>
        {b.new_this_quarter.length > 0 && (
          <ul className="mt-3 space-y-1 text-sm text-slate-300 list-disc pl-5">
            {b.new_this_quarter.map((n) => <li key={n}>{n}</li>)}
          </ul>
        )}
      </Card>
      <Stats s={b.stats} />

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5 items-start">
        <div className="xl:col-span-2 space-y-6">
          {b.storylines.map((s) => (
            <section key={s.title} className="space-y-3">
              <div>
                <h2 className="text-base font-semibold text-white">{s.title}</h2>
                {s.narrative && <p className="text-sm text-slate-300 mt-1 leading-relaxed">{s.narrative}</p>}
              </div>
              {s.findings.map((id) => byId[id] && <FindingCard key={id} f={byId[id]} onAsk={onAsk} />)}
            </section>
          ))}
          {!b.storylines.length && (
            <Card className="p-6 text-sm text-slate-400">No material findings survived review this quarter.</Card>
          )}
        </div>

        <div className="space-y-4">
          <PositionMap points={b.position_map} />
          {b.graded_calls.length > 0 && (
            <Card className="p-4">
              <div className="text-sm font-semibold text-white mb-2">Last quarter's calls</div>
              {b.graded_calls.map((c) => (
                <div key={c.statement} className="text-xs flex gap-2 py-1">
                  <span className={`shrink-0 font-medium ${OUTCOME[c.outcome] || "text-slate-400"}`}>{c.outcome}</span>
                  <span className="text-slate-300">{c.statement}</span>
                </div>
              ))}
            </Card>
          )}
          {b.calls.length > 0 && (
            <Card className="p-4">
              <div className="text-sm font-semibold text-white mb-1">We are watching</div>
              <p className="text-[11px] text-slate-500 mb-2">Graded automatically next quarter.</p>
              {b.calls.map((c) => <div key={c.statement} className="text-xs text-slate-300 py-0.5">• {c.statement}</div>)}
            </Card>
          )}
          {b.trend_shifts.length > 0 && (
            <Card className="p-4">
              <div className="text-sm font-semibold text-white mb-2">Trajectory changes spotted</div>
              {b.trend_shifts.map((t) => (
                <div key={t.entity + t.metric} className="text-xs text-slate-300 py-0.5">
                  <CompanyDot company={t.entity} className="mr-1.5" />
                  {companyName(t.entity)}: {t.label.toLowerCase()} now {t.display} (was {t.prior_display}), reversing its recent direction
                </div>
              ))}
            </Card>
          )}
          {b.ruled_out.length > 0 && (
            <Card className="p-4">
              <div className="text-sm font-semibold text-white mb-2">Looked at and ruled out</div>
              {b.ruled_out.map((r) => (
                <div key={r.hypothesis} className="text-xs py-1">
                  <div className="text-slate-300">{r.hypothesis}</div>
                  <div className="text-slate-500">{r.reason}</div>
                </div>
              ))}
            </Card>
          )}
        </div>
      </div>
      {b.unverified.length > 0 && (
        <p className="text-[11px] text-amber-400">
          Some wording could not be checked against the data: {b.unverified.join(", ")}.
        </p>
      )}
      <p className="text-[11px] text-slate-600">
        Written by {b.model}. Every finding was re-computed from the Data Engine by code and reviewed by a second pass;
        the AI cannot change the source data. Figures are year to date; early-stage insurers are left out of peer comparisons.
      </p>
    </div>
  );
}

function LensFallback({ onAsk }) {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  useEffect(() => {
    API.getGlance(null)
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((error) => setState({ loading: false, data: null, error }));
  }, []);
  if (state.loading) return <LoadingCard label="Working out what matters…" />;
  if (state.error) return <ErrorCard error={state.error} />;
  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
      <div className="space-y-4">
        {state.data.lenses.map((l, i) => <LensCard key={l.id} lens={l} rank={i + 1} onAsk={onAsk} />)}
      </div>
      <PositionMap points={state.data.position_map} />
    </div>
  );
}

export function GlanceView({ onAsk }) {
  const { meta, error: metaError } = useAnalysisMeta();
  const [period, setPeriod] = useState(null);
  const [state, setState] = useState({ loading: true, data: null, error: null });

  const load = useCallback((refresh = false) => {
    return API.getBriefing(period, refresh)
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((error) => setState({ loading: false, data: null, error }));
  }, [period]);

  useEffect(() => { setState({ loading: true, data: null, error: null }); load(); }, [load]);
  useEffect(() => {
    if (state.data?.status !== "running") return undefined;
    const t = setInterval(() => load(), 2000);
    return () => clearInterval(t);
  }, [state.data?.status, load]);

  if (metaError) return <ErrorCard error={metaError} />;
  const d = state.data;
  const b = d?.status === "ready" ? d.briefing : null;

  return (
    <div className="max-w-6xl mx-auto space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">At a glance</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            What an AI analyst found in {b?.period_label ?? "this quarter"} that the report does not show, checked by code.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {b && <Button variant="ghost" onClick={() => load(true)}>Re-run analysis</Button>}
          <PeriodSelect meta={meta} value={period} onChange={setPeriod} />
        </div>
      </div>

      {state.loading && <LoadingCard label="Opening the briefing…" />}
      {state.error && <ErrorCard error={state.error} onRetry={() => load()} />}
      {d?.status === "running" && <InvestigationLog events={d.events} />}
      {d?.status === "error" && (
        <>
          <Card className="p-4 text-sm text-rose-300">The analysis did not complete: {d.error}</Card>
          <Button variant="ghost" onClick={() => load(true)}>Try again</Button>
          <LensFallback onAsk={onAsk} />
        </>
      )}
      {d?.status === "unavailable" && (
        <>
          <Card className="p-4 text-sm text-amber-300">
            The AI analyst is not configured ({d.reason}). Showing the rule-based view instead.
          </Card>
          <LensFallback onAsk={onAsk} />
        </>
      )}
      {b && <Briefing b={b} onAsk={onAsk} />}
    </div>
  );
}
