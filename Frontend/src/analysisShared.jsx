import { useEffect, useState } from "react";
import { API } from "./api.js";
import { AppIcon } from "./Icons.jsx";
import { Card, Spinner } from "./ui.jsx";
import { COMPANY_COLORS, COMPANY_NAMES } from "./charts.jsx";

// ---------------------------------------------------------------------------
// Pieces shared by the Highlights, Competitors and Chat views.
// ---------------------------------------------------------------------------

let metaPromise = null;

/** Analysis metadata (periods, companies, metric catalog, LLM status), fetched
 * once per page load and shared by every analysis view. */
export function useAnalysisMeta() {
  const [state, setState] = useState({ meta: null, error: null });
  useEffect(() => {
    let alive = true;
    metaPromise = metaPromise || API.getAnalysisMeta();
    metaPromise
      .then((meta) => alive && setState({ meta, error: null }))
      .catch((error) => {
        metaPromise = null; // allow a retry on the next mount
        if (alive) setState({ meta: null, error });
      });
    return () => { alive = false; };
  }, []);
  return state;
}

export function PeriodSelect({ meta, value, onChange, includeAnnual = false }) {
  if (!meta) return null;
  const options = [...meta.quarters].reverse().concat(includeAnnual ? [...meta.annual].reverse() : []);
  return (
    <select
      value={value || meta.latest_quarter}
      onChange={(e) => onChange(e.target.value)}
      aria-label="Period"
      className="bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-sm text-slate-200 outline-none focus:border-brand-500"
    >
      {options.map((p) => (
        <option key={p.id} value={p.id}>{p.label}</option>
      ))}
    </select>
  );
}

/** A signed change with a direction-aware colour: green when it moved the
 * good way for this metric, red when bad, grey when neutral/unknown. */
export function Delta({ change, improved, className = "" }) {
  if (!change) return <span className={`text-slate-600 ${className}`}>–</span>;
  const tone = improved === true ? "text-emerald-400" : improved === false ? "text-rose-400" : "text-slate-400";
  const arrow = change.abs > 0 ? "▲" : change.abs < 0 ? "▼" : "";
  return (
    <span className={`tabular-nums text-xs ${tone} ${className}`}>
      {arrow && <span className="text-[9px] mr-0.5">{arrow}</span>}
      {change.display}
    </span>
  );
}

export function CompanyDot({ company, className = "" }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${className}`}
          style={{ background: COMPANY_COLORS[company] || "#64748b" }} />
  );
}

export const companyName = (k) => COMPANY_NAMES[k] || k;

export function LoadingCard({ label = "Loading…" }) {
  return (
    <Card className="p-10 flex items-center justify-center gap-2 text-sm text-slate-400">
      <Spinner /> {label}
    </Card>
  );
}

export function ErrorCard({ error, onRetry }) {
  const noData = error?.status === 404;
  return (
    <Card className="p-8 text-center">
      <AppIcon name="alertTriangle" className={`w-6 h-6 mx-auto mb-2 ${noData ? "text-slate-500" : "text-rose-400"}`} />
      <div className="text-sm text-slate-200">{noData ? "No data yet" : "Couldn't load this view"}</div>
      <div className="text-xs text-slate-500 mt-1">{error?.message}</div>
      {onRetry && (
        <button onClick={onRetry} className="mt-3 text-xs text-brand-300 hover:text-brand-200">Try again</button>
      )}
    </Card>
  );
}

/** Amber banner listing data-quality warnings (duplicated companies,
 * implausible jumps) so nobody quotes a glitch as a finding. */
export function DataChecks({ checks }) {
  const [open, setOpen] = useState(false);
  if (!checks?.length) return null;
  const shown = open ? checks : checks.slice(0, 1);
  return (
    <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 px-4 py-3">
      <div className="flex items-start gap-2.5">
        <AppIcon name="alertTriangle" className="w-4 h-4 text-amber-400 mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-xs font-semibold text-amber-200">
            Data checks · {checks.length} warning{checks.length > 1 ? "s" : ""}
          </div>
          <ul className="mt-1 space-y-1 text-xs text-amber-100/80">
            {shown.map((c, i) => <li key={i}>{c.message || c}</li>)}
          </ul>
          {checks.length > 1 && (
            <button onClick={() => setOpen(!open)} className="mt-1.5 text-[11px] text-amber-300 hover:text-amber-200">
              {open ? "Show less" : `Show all ${checks.length}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Small chip for a cited data point; links to the source filing when known. */
export function CitationChip({ c }) {
  const body = (
    <>
      <CompanyDot company={c.entity} />
      <span className="text-slate-400">{companyName(c.entity)}</span>
      <span className="text-slate-300">{c.label}</span>
      <span className="text-slate-100 tabular-nums">{c.display}</span>
      <span className="text-slate-500">{c.period_label}</span>
    </>
  );
  const cls = "inline-flex items-center gap-1.5 rounded-md border border-slate-800 bg-slate-900 px-2 py-1 text-[11px]";
  return c.source_link ? (
    <a href={c.source_link} target="_blank" rel="noopener noreferrer" title={`Source: ${c.source}`}
       className={`${cls} hover:border-slate-600`}>
      {body}
      <AppIcon name="arrowRight" className="w-3 h-3 text-slate-500 -rotate-45" />
    </a>
  ) : (
    <span title={`Source: ${c.source}`} className={cls}>{body}</span>
  );
}
