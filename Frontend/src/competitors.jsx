import { useCallback, useEffect, useState } from "react";
import { API } from "./api.js";
import { AppIcon } from "./Icons.jsx";
import { Badge, Button, Card } from "./ui.jsx";
import { COMPANY_COLORS, ChartBlock } from "./charts.jsx";
import {
  CompanyDot, DataChecks, Delta, ErrorCard, LoadingCard, PeriodSelect, companyName, useAnalysisMeta,
} from "./analysisShared.jsx";

// ---------------------------------------------------------------------------
// Competitor deep-dive: one insurer's KPIs, where it leads/lags its peers,
// the insights about it, its mix vs. last year and vs. SAHI, full-year
// trends against the peer median, and every metric with its rank.
// ---------------------------------------------------------------------------

const PRIOR_COLOR = "#64748b";
const SAHI_COLOR = "#94a3b8";

const shortFy = (p) => {
  const m = /^FY\d{2}-(\d{2})$/.exec(p);
  return m ? `FY${m[1]}` : p;
};

function Kpi({ k }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-3 min-w-0">
      <div className="text-[11px] text-slate-400 truncate" title={k.label}>{k.label}</div>
      <div className="text-lg font-semibold text-white tabular-nums mt-0.5">{k.display}</div>
      <div className="flex items-center gap-2 mt-0.5">
        <Delta change={k.change} improved={k.improved} />
        {k.rank && <span className="text-[10px] text-slate-500">#{k.rank} of {k.of}</span>}
      </div>
    </div>
  );
}

function RankList({ title, rows, tone }) {
  return (
    <Card className="p-4">
      <div className="flex items-center gap-2 mb-2">
        <AppIcon name={tone === "good" ? "checkCircle" : "alertTriangle"}
                 className={`w-4 h-4 ${tone === "good" ? "text-emerald-400" : "text-rose-400"}`} />
        <h3 className="text-sm font-semibold text-white">{title}</h3>
      </div>
      {rows.length === 0 && <div className="text-xs text-slate-500">Nothing stands out.</div>}
      <ul className="space-y-1.5">
        {rows.map((r) => (
          <li key={r.metric} className="flex items-center gap-2 text-xs">
            <span className={`w-6 shrink-0 tabular-nums text-right ${tone === "good" ? "text-emerald-300" : "text-rose-300"}`}>#{r.rank}</span>
            <span className="text-slate-300 truncate flex-1" title={r.label}>{r.label}</span>
            <span className="text-slate-100 tabular-nums">{r.display}</span>
            <span className="text-slate-500 tabular-nums w-28 shrink-0 text-right whitespace-nowrap" title="Peer median">
              med {r.peer_median_display}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function breakdownSpec(b, d) {
  return {
    type: "grouped_bar", title: b.label, unit: b.unit,
    categories: b.rows.map((r) => r.dimension),
    series: [
      { name: d.prior_period_label, values: b.rows.map((r) => r.prior), color: PRIOR_COLOR },
      { name: d.period_label, values: b.rows.map((r) => r.value), color: COMPANY_COLORS[d.company] },
      ...(b.rows.some((r) => r.sahi != null)
        ? [{ name: "SAHI", values: b.rows.map((r) => r.sahi), color: SAHI_COLOR }] : []),
    ],
  };
}

function trendSpec(t, company) {
  const s = t.series[0];
  return {
    type: "line", title: t.label, unit: t.unit,
    categories: t.periods.map(shortFy),
    series: [
      { name: company, values: s.points.map((p) => p.value) },
      { name: "Peer median", values: t.peer_median, color: SAHI_COLOR, dashed: true },
    ],
  };
}

function MetricTable({ sections }) {
  const [open, setOpen] = useState(() => new Set(sections.slice(0, 2).map((s) => s.section)));
  const toggle = (s) => setOpen((o) => { const n = new Set(o); n.has(s) ? n.delete(s) : n.add(s); return n; });
  return (
    <Card className="divide-y divide-slate-800">
      {sections.map((sec) => (
        <div key={sec.section}>
          <button onClick={() => toggle(sec.section)}
                  className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-slate-200 hover:bg-slate-900/80">
            <AppIcon name="chevronDown" className={`w-4 h-4 text-slate-500 transition-transform ${open.has(sec.section) ? "" : "-rotate-90"}`} />
            {sec.section}
            <span className="text-xs text-slate-500">{sec.rows.length}</span>
          </button>
          {open.has(sec.section) && (
            <div className="overflow-x-auto px-4 pb-3">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-slate-500">
                    <th className="text-left font-medium py-1">Metric</th>
                    <th className="text-right font-medium py-1">Last year</th>
                    <th className="text-right font-medium py-1">This period</th>
                    <th className="text-right font-medium py-1">YoY</th>
                    <th className="text-right font-medium py-1">Rank</th>
                    <th className="text-right font-medium py-1">Peer median</th>
                    <th className="text-right font-medium py-1">SAHI</th>
                  </tr>
                </thead>
                <tbody>
                  {sec.rows.map((r) => (
                    <tr key={r.metric} className="border-t border-slate-800/60">
                      <td className="py-1.5 text-slate-300">{r.label}</td>
                      <td className="py-1.5 text-right tabular-nums text-slate-500">{r.prior_display ?? "–"}</td>
                      <td className="py-1.5 text-right tabular-nums text-slate-100">{r.display}</td>
                      <td className="py-1.5 text-right"><Delta change={r.change} improved={r.improved} /></td>
                      <td className="py-1.5 text-right tabular-nums text-slate-400">{r.rank ? `${r.rank}/${r.of}` : "–"}</td>
                      <td className="py-1.5 text-right tabular-nums text-slate-400">{r.peer_median_display}</td>
                      <td className="py-1.5 text-right tabular-nums text-slate-400">{r.sahi_display ?? "–"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </Card>
  );
}

export function CompetitorsView({ onAsk }) {
  const { meta, error: metaError } = useAnalysisMeta();
  const [company, setCompany] = useState("STAR");
  const [period, setPeriod] = useState(null);
  const [state, setState] = useState({ loading: true, data: null, error: null });

  const load = useCallback(() => {
    setState((s) => ({ loading: true, data: s.data, error: null }));
    API.getDeepDive(company, period)
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((error) => setState({ loading: false, data: null, error }));
  }, [company, period]);

  useEffect(() => { load(); }, [load]);

  if (metaError) return <ErrorCard error={metaError} />;
  const d = state.data;
  const companies = meta?.companies || [];

  return (
    <div className="max-w-6xl mx-auto space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Competitor deep-dive</h1>
          <p className="text-sm text-slate-400 mt-0.5">Every metric for one insurer, ranked against its peers.</p>
        </div>
        <div className="ml-auto"><PeriodSelect meta={meta} value={period} onChange={setPeriod} includeAnnual /></div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {companies.map((c) => (
          <button key={c.key} onClick={() => setCompany(c.key)}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm transition-colors ${
                    company === c.key ? "border-brand-500/50 bg-brand-500/15 text-white" : "border-slate-800 text-slate-400 hover:text-white"
                  }`}>
            <CompanyDot company={c.key} /> {c.name}
          </button>
        ))}
      </div>

      {state.loading && !d && <LoadingCard />}
      {state.error && <ErrorCard error={state.error} onRetry={load} />}
      {d && (
        <div className={`space-y-5 transition-opacity ${state.loading ? "opacity-50" : ""}`}>
          <div className="flex flex-wrap items-center gap-3">
            <span className="w-3 h-3 rounded-full" style={{ background: COMPANY_COLORS[d.company] }} />
            <h2 className="text-lg font-semibold text-white">{d.name}</h2>
            <span className="text-sm text-slate-400">{d.period_label} vs {d.prior_period_label}</span>
            {d.early_stage && <Badge tone="amber">Early-stage insurer · small base</Badge>}
            <Button variant="subtle" className="ml-auto"
                    onClick={() => onAsk(`Give me a competitive assessment of ${d.name} for ${d.period_label}: strengths, weaknesses, what changed and why, versus peers.`)}>
              <AppIcon name="edit" className="w-4 h-4" /> Ask about {companyName(d.company)}
            </Button>
          </div>

          <DataChecks checks={d.data_checks} />

          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
            {d.kpis.map((k) => <Kpi key={k.metric} k={k} />)}
          </div>

          {d.insights.length > 0 && (
            <Card className="p-4">
              <h3 className="text-sm font-semibold text-white mb-2">What's happening</h3>
              <ul className="space-y-2">
                {d.insights.map((i) => (
                  <li key={i.id} className="text-sm">
                    <div className="text-slate-100">{i.headline}</div>
                    {i.implication && <div className="text-xs text-slate-400 mt-0.5">{i.implication}</div>}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <RankList title="Where it leads" rows={d.strengths} tone="good" />
            <RankList title="Where it lags" rows={d.weaknesses} tone="bad" />
          </div>

          {d.breakdowns.length > 0 && (
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
              {d.breakdowns.map((b) => (
                <Card key={b.metric} className="p-4"><ChartBlock spec={breakdownSpec(b, d)} height={220} /></Card>
              ))}
            </div>
          )}

          {d.trends.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-white mb-2">Full-year trends vs. peer median</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                {d.trends.map((t) => (
                  <Card key={t.metric} className="p-3"><ChartBlock spec={trendSpec(t, d.company)} height={170} /></Card>
                ))}
              </div>
            </div>
          )}

          <MetricTable key={d.company + d.period} sections={d.sections} />
        </div>
      )}
    </div>
  );
}
