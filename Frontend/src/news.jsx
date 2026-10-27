import { useCallback, useEffect, useState } from "react";
import { API } from "./api.js";
import { AppIcon } from "./Icons.jsx";
import { Badge, Button, Card, Spinner } from "./ui.jsx";
import { CompanyDot, ErrorCard, LoadingCard, companyName } from "./analysisShared.jsx";

// ---------------------------------------------------------------------------
// Competitor News: recent articles per insurer, one compact row each. Fetched
// by the backend (Parallel search, Gemini summary + tag) and cached there, so
// opening the tab is cheap; Refresh forces a re-fetch.
// ---------------------------------------------------------------------------

const COMPANIES = ["NBHI", "STAR", "CARE", "CIGNA", "ABHI", "NARAYANA", "GALAXY"];
const TAG_TONE = {
  Regulatory: "amber", Financial: "emerald", Product: "sky",
  "Claims/Customer": "rose", Partnership: "violet", Leadership: "slate", Other: "slate",
};
const COLLAPSED_ROWS = 4;

const pillClass = (active) =>
  `rounded-full border px-3 py-1 text-xs transition-colors ${
    active ? "border-brand-500/50 bg-brand-500/15 text-white" : "border-slate-800 text-slate-400 hover:text-white"
  }`;

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

function ArticleRow({ item }) {
  const when = relativeTime(item.publish_date);
  const meta = [item.source, when].filter(Boolean).join(" · ");
  return (
    <li className="py-2 first:pt-0 last:pb-0">
      <div className="flex items-start gap-2">
        <a href={item.url} target="_blank" rel="noopener noreferrer" title={item.title}
           className="min-w-0 flex-1 text-sm text-slate-100 hover:text-brand-300 line-clamp-1">
          {item.title}
        </a>
        {item.tag && <Badge tone={TAG_TONE[item.tag] || "slate"}>{item.tag}</Badge>}
      </div>
      <div className="text-[11px] text-slate-500 line-clamp-1" title={item.summary || meta}>
        {meta}{item.summary ? ` — ${item.summary}` : ""}
      </div>
    </li>
  );
}

function CompanyNewsCard({ entry, tag }) {
  const [expanded, setExpanded] = useState(false);
  const items = entry.items.filter((i) => tag === "All" || i.tag === tag);
  const shown = expanded ? items : items.slice(0, COLLAPSED_ROWS);
  const hidden = items.length - shown.length;
  return (
    <Card className="p-4">
      <div className="flex items-center gap-2 mb-3">
        <CompanyDot company={entry.company} />
        <h2 className="text-sm font-semibold text-white">{companyName(entry.company)}</h2>
        <span className="ml-auto"><Badge>{items.length}</Badge></span>
      </div>
      {entry.error && (
        <div className="text-xs text-rose-300 mb-2">Couldn't fetch news: {entry.error}</div>
      )}
      {items.length === 0 && !entry.error && (
        <div className="text-xs text-slate-500">No recent news{tag !== "All" ? ` tagged ${tag}` : ""}.</div>
      )}
      {items.length > 0 && (
        <ul className="divide-y divide-slate-800/70">
          {shown.map((i) => <ArticleRow key={i.url} item={i} />)}
        </ul>
      )}
      {(hidden > 0 || expanded) && items.length > COLLAPSED_ROWS && (
        <button onClick={() => setExpanded((e) => !e)}
                className="mt-2 text-xs text-brand-300 hover:text-brand-200">
          {expanded ? "Show less" : `Show ${hidden} more`}
        </button>
      )}
    </Card>
  );
}

export function NewsView() {
  const [company, setCompany] = useState("All");
  const [tag, setTag] = useState("All");
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback((refresh = false) => {
    if (refresh) setRefreshing(true);
    else setState({ loading: true, data: null, error: null });
    API.getNews(null, refresh)
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((error) => setState({ loading: false, data: null, error }))
      .finally(() => setRefreshing(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const data = state.data;
  const entries = data ? data.companies.filter((c) => company === "All" || c.company === company) : [];
  const tags = data
    ? Object.keys(TAG_TONE).filter((t) => data.companies.some((c) => c.items.some((i) => i.tag === t)))
    : [];
  const fetchedAt = data && data.companies.map((c) => c.fetched_at).filter(Boolean).sort()[0];

  return (
    <div className="max-w-6xl mx-auto space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Competitor news</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Last {data?.lookback_days ?? 30} days
            {fetchedAt ? ` · updated ${relativeTime(fetchedAt) === "today" ? "today" : relativeTime(fetchedAt)}` : ""}
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
          <div className="flex flex-wrap gap-1.5">
            {["All", ...COMPANIES].map((c) => (
              <button key={c} onClick={() => setCompany(c)} className={pillClass(company === c)}>
                {c === "All" ? "All" : companyName(c)}
              </button>
            ))}
          </div>
          {tags.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {["All", ...tags].map((t) => (
                <button key={t} onClick={() => setTag(t)} className={pillClass(tag === t)}>
                  {t === "All" ? "All topics" : t}
                </button>
              ))}
            </div>
          )}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {entries.map((e) => <CompanyNewsCard key={e.company} entry={e} tag={tag} />)}
          </div>
        </>
      )}
    </div>
  );
}
