import { useCallback, useEffect, useState } from "react";
import { API } from "./api.js";
import { AppIcon } from "./Icons.jsx";
import { Badge, Button, Card, Spinner } from "./ui.jsx";
import { CompanyDot, ErrorCard, LoadingCard, companyName } from "./analysisShared.jsx";

// ---------------------------------------------------------------------------
// Competitor News: the top-ranked recent articles across the competitors
// (ranked by the backend: importance + recency, capped per insurer). Each is a
// block with the headline and a short description. Refresh forces a re-fetch.
// ---------------------------------------------------------------------------

const TAG_TONE = {
  Regulatory: "amber", Financial: "emerald", Product: "sky",
  "Claims/Customer": "rose", Partnership: "violet", Leadership: "slate", Other: "slate",
};

function relativeTime(iso) {
  if (!iso) return null;
  const t = new Date(iso.length <= 10 ? `${iso}T00:00:00` : iso).getTime();
  if (Number.isNaN(t)) return null;
  const days = Math.floor((Date.now() - t) / 86400000);
  if (days < 1) return "today";
  if (days === 1) return "1d ago";
  if (days < 30) return `${days}d ago`;
  return new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function NewsBlock({ item }) {
  const when = relativeTime(item.publish_date);
  return (
    <Card className="p-4 flex flex-col gap-2">
      <div className="flex items-center gap-2 flex-wrap">
        {item.companies.map((c) => (
          <span key={c} className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-200">
            <CompanyDot company={c} /> {companyName(c)}
          </span>
        ))}
        {item.tag && <span className="ml-auto"><Badge tone={TAG_TONE[item.tag] || "slate"}>{item.tag}</Badge></span>}
      </div>
      <a href={item.url} target="_blank" rel="noopener noreferrer"
         className="text-sm font-semibold text-white hover:text-brand-300 line-clamp-2">
        {item.title}
      </a>
      {item.description && <p className="text-sm text-slate-400 line-clamp-3">{item.description}</p>}
      <div className="mt-auto pt-1 text-[11px] text-slate-500">
        {[item.source, when].filter(Boolean).join(" · ")}
      </div>
    </Card>
  );
}

export function NewsView() {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback((refresh = false) => {
    if (refresh) setRefreshing(true);
    else setState({ loading: true, data: null, error: null });
    API.getNews(refresh)
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((error) => setState({ loading: false, data: null, error }))
      .finally(() => setRefreshing(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const data = state.data;
  const updated = data?.fetched_at ? relativeTime(data.fetched_at) : null;

  return (
    <div className="max-w-6xl mx-auto space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Competitor news</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Top stories from the last {data?.lookback_days ?? 30} days, ranked by importance and recency
            {updated ? ` · updated ${updated}` : ""}
          </p>
        </div>
        <div className="ml-auto">
          <Button variant="ghost" onClick={() => load(true)} disabled={refreshing || state.loading}>
            {refreshing ? <Spinner /> : <AppIcon name="refresh" className="w-4 h-4" />} Refresh
          </Button>
        </div>
      </div>

      {state.loading && <LoadingCard label="Fetching the latest news…" />}
      {state.error && <ErrorCard error={state.error} onRetry={() => load()} />}
      {data && (
        <>
          {data.errors.length > 0 && (
            <div className="text-xs text-rose-300">
              Couldn't fetch news for {data.errors.map((e) => companyName(e.company)).join(", ")}
              {data.items.length === 0 ? `: ${data.errors[0].error}` : ""}
            </div>
          )}
          {data.items.length === 0 && data.errors.length === 0 && (
            <Card className="p-8 text-center text-sm text-slate-400">No recent competitor news found.</Card>
          )}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {data.items.map((i) => <NewsBlock key={i.url} item={i} />)}
          </div>
          {data.items.length > 0 && !data.summarised && (
            <p className="text-[11px] text-slate-500">
              AI summaries unavailable - showing excerpts, ranked by recency only.
            </p>
          )}
        </>
      )}
    </div>
  );
}
