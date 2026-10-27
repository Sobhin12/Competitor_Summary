import { useCallback, useEffect, useState } from "react";
import { API } from "./api.js";
import { AppIcon } from "./Icons.jsx";
import { Badge, Button, Card, Spinner } from "./ui.jsx";
import { COMPANY_COLORS, ChartBlock } from "./charts.jsx";
import { Markdown } from "./markdown.jsx";
import {
  CompanyDot, DataChecks, Delta, ErrorCard, LoadingCard, PeriodSelect, companyName, useAnalysisMeta,
} from "./analysisShared.jsx";

// ---------------------------------------------------------------------------
// Quarter Highlights: computed insights (what moved, the drivers behind it,
// context, a rule-based implication) plus an AI-written narrative across
// them, kept visibly separate - cards are data, the narrative is
// interpretation.
// ---------------------------------------------------------------------------

const THEMES = ["Market", "Growth & market share", "Profitability", "Earnings", "Portfolio mix", "Distribution", "Capital"];
const SENTIMENT = {
  positive: { dot: "bg-emerald-400", tone: "emerald" },
  negative: { dot: "bg-rose-400", tone: "rose" },
  neutral: { dot: "bg-slate-400", tone: "slate" },
};

function Narrative({ period }) {
  const [state, setState] = useState({ loading: true, data: null, error: null });

  const load = useCallback((refresh = false) => {
    setState((s) => ({ ...s, loading: true, error: null }));
    API.getNarrative(period, refresh)
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((error) => setState({ loading: false, data: null, error }));
  }, [period]);

  useEffect(() => { load(); }, [load]);

  return (
    <Card className="p-5">
      <div className="flex items-center gap-2 mb-3">
        <AppIcon name="edit" className="w-4 h-4 text-violet-300" />
        <h2 className="text-sm font-semibold text-white">Executive narrative</h2>
        <Badge tone="violet">Interpretation · AI-written</Badge>
        <div className="ml-auto flex items-center gap-2">
          {state.data && (
            <span className="text-[11px] text-slate-500">
              {state.data.provider} · {state.data.model}{state.data.cached ? " · cached" : ""}
            </span>
          )}
          <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => load(true)} disabled={state.loading}
                  title="Regenerate">
            {state.loading ? <Spinner className="w-3.5 h-3.5" /> : <AppIcon name="refresh" className="w-3.5 h-3.5" />}
          </Button>
        </div>
      </div>
      {state.loading && !state.data && (
        <div className="flex items-center gap-2 text-sm text-slate-400 py-4"><Spinner /> Writing the narrative…</div>
      )}
      {state.error && (
        <div className="text-xs text-slate-400 py-2">
          The AI narrative isn't available right now ({state.error.message}). The computed insights below don't depend on it.
        </div>
      )}
      {state.data && (
        <>
          <Markdown text={state.data.markdown} unverified={state.data.unverified} />
          <p className="text-[11px] text-slate-500 mt-3">
            Written from the insight cards below; every figure is checked against them.
            {state.data.unverified?.length > 0 && (
              <span className="text-amber-300"> {state.data.unverified.length} figure(s) couldn't be traced and are underlined.</span>
            )}
          </p>
        </>
      )}
    </Card>
  );
}

function ShareStrip({ share }) {
  if (!share?.rows?.length) return null;
  return (
    <Card className="p-4">
      <div className="flex items-baseline gap-2 mb-3">
        <h2 className="text-sm font-semibold text-white">SAHI market share</h2>
        <span className="text-xs text-slate-500">
          of {share.base_total_display}
          {share.base_growth_display ? ` · market ${share.base_growth_display} YoY` : ""}
        </span>
      </div>
      <div className="flex h-3 rounded-full overflow-hidden gap-[2px] bg-slate-950">
        {share.rows.map((r) => (
          <div key={r.entity} title={`${companyName(r.entity)} ${r.display}`}
               style={{ width: `${(r.share || 0) * 100}%`, background: COMPANY_COLORS[r.entity] }}
               className="h-full" />
        ))}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3 mt-3">
        {share.rows.map((r) => (
          <div key={r.entity} className="min-w-0">
            <div className="flex items-center gap-1.5 text-[11px] text-slate-400 truncate">
              <CompanyDot company={r.entity} /> {companyName(r.entity)}
            </div>
            <div className="text-sm text-white tabular-nums">{r.display}</div>
            <Delta change={r.change} improved={r.change ? r.change.abs > 0 : null} />
          </div>
        ))}
      </div>
    </Card>
  );
}

function InsightCard({ ins, onAsk }) {
  const s = SENTIMENT[ins.sentiment] || SENTIMENT.neutral;
  const [more, setMore] = useState(false);
  return (
    <Card className="p-4 flex flex-col gap-3">
      <div className="flex items-center gap-2 text-[11px]">
        <span className={`w-1.5 h-1.5 rounded-full ${s.dot}`} />
        <span className="text-slate-400">{ins.theme}</span>
        {ins.entity && ins.entity !== "SAHI" && (
          <span className="flex items-center gap-1 text-slate-300"><CompanyDot company={ins.entity} />{ins.entity_name}</span>
        )}
      </div>
      <h3 className="text-sm font-semibold text-white leading-snug">{ins.headline}</h3>

      {ins.drivers?.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-1">What drove it</div>
          <div className="flex flex-wrap gap-1.5">
            {ins.drivers.slice(0, 6).map((d, i) => (
              <span key={i} className="rounded-md bg-slate-800/70 px-2 py-0.5 text-[11px] text-slate-300">
                {d.factor} <span className={`tabular-nums ${d.contribution > 0 ? "text-sky-300" : d.contribution < 0 ? "text-orange-300" : ""}`}>{d.display}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {ins.chart && <ChartBlock spec={ins.chart} height={170} compact />}

      {ins.implication && (
        <div className="rounded-lg bg-brand-500/5 border border-brand-500/15 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wide text-brand-300 mb-0.5">So what</div>
          <div className="text-xs text-slate-200 leading-relaxed">{ins.implication}</div>
        </div>
      )}

      {ins.caveats?.map((c, i) => (
        <div key={i} className="flex items-start gap-1.5 text-[11px] text-amber-200/80">
          <AppIcon name="alertTriangle" className="w-3 h-3 mt-0.5 shrink-0 text-amber-400" /> {c}
        </div>
      ))}

      {(ins.context?.length > 0 || ins.evidence?.length > 0) && more && (
        <div className="space-y-2 text-xs">
          {ins.context?.length > 0 && (
            <ul className="list-disc pl-4 text-slate-400 space-y-0.5">
              {ins.context.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          )}
          {ins.evidence?.length > 0 && (
            <table className="w-full">
              <tbody>
                {ins.evidence.map((e, i) => (
                  <tr key={i} className="border-t border-slate-800/70">
                    <td className="py-1 pr-2 text-slate-400">{e.label} <span className="text-slate-600">· {companyName(e.entity)}</span></td>
                    <td className="py-1 pr-2 text-right tabular-nums text-slate-500">{e.prior_display ?? "–"}</td>
                    <td className="py-1 pr-2 text-right tabular-nums text-slate-100">{e.display}</td>
                    <td className="py-1 text-right tabular-nums text-slate-400">{e.change_display ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div className="flex items-center gap-3 mt-auto pt-1">
        {(ins.context?.length > 0 || ins.evidence?.length > 0) && (
          <button onClick={() => setMore(!more)} className="text-[11px] text-slate-400 hover:text-white">
            {more ? "Hide evidence" : "Evidence & context"}
          </button>
        )}
        <button
          onClick={() => onAsk(`Tell me more about this: "${ins.headline}". What's behind it, and how does it compare with peers?`)}
          className="ml-auto inline-flex items-center gap-1 text-[11px] text-brand-300 hover:text-brand-200"
        >
          Ask more <AppIcon name="arrowRight" className="w-3 h-3" />
        </button>
      </div>
    </Card>
  );
}

export function HighlightsView({ onAsk }) {
  const { meta, error: metaError } = useAnalysisMeta();
  const [period, setPeriod] = useState(null);
  const [theme, setTheme] = useState("All");
  const [state, setState] = useState({ loading: true, data: null, error: null });

  const load = useCallback(() => {
    setState({ loading: true, data: null, error: null });
    API.getHighlights(period)
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((error) => setState({ loading: false, data: null, error }));
  }, [period]);

  useEffect(() => { load(); }, [load]);

  if (metaError) return <ErrorCard error={metaError} />;
  const data = state.data;
  const themes = data ? THEMES.filter((t) => data.insights.some((i) => i.theme === t)) : [];
  const shown = data ? data.insights.filter((i) => theme === "All" || i.theme === theme) : [];

  return (
    <div className="max-w-6xl mx-auto space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Quarter highlights</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            What moved{data ? ` in ${data.period_label} vs ${data.prior_period_label}` : ""}, what drove it, and why it matters.
          </p>
        </div>
        <div className="ml-auto"><PeriodSelect meta={meta} value={period} onChange={setPeriod} /></div>
      </div>

      {state.loading && <LoadingCard label="Analysing the quarter…" />}
      {state.error && <ErrorCard error={state.error} onRetry={load} />}
      {data && (
        <>
          <DataChecks checks={data.data_checks} />
          {meta?.llm?.available && <Narrative period={data.period} />}
          <ShareStrip share={data.market_share} />

          <div className="flex flex-wrap gap-1.5">
            {["All", ...themes].map((t) => (
              <button key={t} onClick={() => setTheme(t)}
                      className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                        theme === t ? "border-brand-500/50 bg-brand-500/15 text-white" : "border-slate-800 text-slate-400 hover:text-white"
                      }`}>
                {t}
              </button>
            ))}
            <span className="ml-auto self-center text-[11px] text-slate-500">
              Computed from the Data Engine · {data.insights.length} of {data.all_insights_count} insights shown
            </span>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {shown.map((ins) => <InsightCard key={ins.id} ins={ins} onAsk={onAsk} />)}
          </div>
        </>
      )}
    </div>
  );
}
