import { useEffect, useRef, useState } from "react";
import { API } from "./api.js";
import { AppIcon } from "./Icons.jsx";
import { Badge, Button, Card, Spinner } from "./ui.jsx";
import { ChartBlock, TableBlock } from "./charts.jsx";
import { Markdown } from "./markdown.jsx";
import { CitationChip, ErrorCard, PeriodSelect, useAnalysisMeta } from "./analysisShared.jsx";

// ---------------------------------------------------------------------------
// Chat over the Data Engine. The backend agent calls deterministic tools for
// every figure; this view streams its answer, renders the charts/tables it
// emits, underlines any number it couldn't trace to the data, and lists the
// data points it cited (linked to the source filing).
//
// Message state lives in App so a conversation survives switching views.
// ---------------------------------------------------------------------------

const STARTERS = [
  "Who gained and who lost SAHI market share this quarter, and why?",
  "Compare combined ratios across insurers and explain what's driving the gaps.",
  "How is Star Health's profitability trending, and what's behind it?",
  "Which insurers are most dependent on brokers? Show a chart.",
  "Show GWP growth for all insurers since FY18.",
  "Which insurers look capital-constrained?",
];

const TOOL_LABELS = {
  list_metrics: "Searched metrics", list_periods: "Checked periods", list_companies: "Listed companies",
  get_values: "Looked up values", compare: "Compared insurers", breakdown: "Broke down a mix",
  trend: "Pulled a trend", top_movers: "Found top movers", market_share: "Computed market share",
  highlights: "Read the insights", company_profile: "Read a company profile", calculate: "Calculated",
  render_chart: "Drew a chart", render_table: "Built a table",
};

function AssistantMessage({ m }) {
  const v = m.verification;
  return (
    <div className="space-y-3">
      {m.tools?.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {m.tools.map((t, i) => (
            <span key={i} title={JSON.stringify(t.args)}
                  className="inline-flex items-center gap-1 rounded-md bg-slate-800/60 px-2 py-0.5 text-[11px] text-slate-400">
              <AppIcon name="cpu" className="w-3 h-3" />
              {TOOL_LABELS[t.name] || t.name}
              {t.args?.metric ? ` · ${t.args.metric}` : t.args?.company ? ` · ${t.args.company}` : ""}
            </span>
          ))}
        </div>
      )}
      {m.content && <Markdown text={m.content} unverified={v?.unverified} />}
      {m.blocks?.map((b, i) => (
        <div key={i} className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
          {b.type === "chart" ? <ChartBlock spec={b.spec} /> : <TableBlock spec={b.spec} />}
        </div>
      ))}
      {m.pending && !m.content && (
        <div className="flex items-center gap-2 text-sm text-slate-400"><Spinner /> Analysing…</div>
      )}
      {m.error && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-200">{m.error}</div>
      )}
      {v && (
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          {v.unverified.length === 0 ? (
            <Badge tone="emerald"><AppIcon name="checkCircle" className="w-3 h-3" /> All {v.checked} figures traced to the data</Badge>
          ) : (
            <Badge tone="amber">
              <AppIcon name="alertTriangle" className="w-3 h-3" />
              {v.unverified.length} of {v.checked} figures not traceable - underlined
            </Badge>
          )}
        </div>
      )}
      {m.citations?.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer text-[11px] text-slate-500 hover:text-slate-300 list-none">
            <span className="group-open:hidden">Show {m.citations.length} sources ▸</span>
            <span className="hidden group-open:inline">Hide sources ▾</span>
          </summary>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {m.citations.map((c) => <CitationChip key={c.id} c={c} />)}
          </div>
        </details>
      )}
    </div>
  );
}

export function ChatView({ messages, setMessages, seed, onSeedUsed }) {
  const { meta, error: metaError } = useAnalysisMeta();
  const [input, setInput] = useState("");
  const [period, setPeriod] = useState(null);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  // Abort an in-flight answer if the view unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  async function send(text) {
    const question = (text ?? input).trim();
    if (!question || busy) return;
    setInput("");
    const history = [...messages.filter((m) => m.content && !m.error).map((m) => ({ role: m.role, content: m.content })),
                     { role: "user", content: question }];
    const assistant = { role: "assistant", content: "", tools: [], blocks: [], pending: true };
    setMessages((ms) => [...ms, { role: "user", content: question }, assistant]);
    setBusy(true);

    const update = (fn) => setMessages((ms) => {
      const copy = ms.slice();
      copy[copy.length - 1] = fn({ ...copy[copy.length - 1] });
      return copy;
    });

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await API.streamChat(history, period, (event, data) => {
        if (event === "text") update((m) => ({ ...m, content: m.content + data }));
        else if (event === "tool_status") update((m) => ({ ...m, tools: [...m.tools, data] }));
        else if (event === "chart") update((m) => ({ ...m, blocks: [...m.blocks, { type: "chart", spec: data }] }));
        else if (event === "table") update((m) => ({ ...m, blocks: [...m.blocks, { type: "table", spec: data }] }));
        else if (event === "verification") update((m) => ({ ...m, verification: data }));
        else if (event === "citations") update((m) => ({ ...m, citations: data }));
        else if (event === "error") update((m) => ({ ...m, error: data.message }));
      }, controller.signal);
    } catch (e) {
      if (e?.name !== "AbortError") update((m) => ({ ...m, error: e.message }));
    } finally {
      update((m) => ({ ...m, pending: false, content: m.content.trim() }));
      setBusy(false);
      abortRef.current = null;
    }
  }

  // A question handed over from Highlights / Competitors ("Ask more"). The
  // ref guards against StrictMode's double effect run sending it twice.
  const usedSeed = useRef(null);
  useEffect(() => {
    if (seed?.text && meta && usedSeed.current !== seed.nonce) {
      usedSeed.current = seed.nonce;
      onSeedUsed();
      send(seed.text);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed?.nonce, meta]);

  if (metaError) return <ErrorCard error={metaError} />;
  const llm = meta?.llm;

  return (
    <div className="max-w-4xl mx-auto h-full flex flex-col">
      <div className="flex flex-wrap items-end gap-3 mb-4">
        <div>
          <h1 className="text-xl font-semibold text-white">Ask the data</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Answers come only from the Data Engine. Figures that can't be traced back are underlined.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {llm?.available && <span className="text-[11px] text-slate-500">{llm.provider} · {llm.model}</span>}
          <PeriodSelect meta={meta} value={period} onChange={setPeriod} />
          {messages.length > 0 && (
            <Button variant="ghost" onClick={() => setMessages([])} disabled={busy}>New chat</Button>
          )}
        </div>
      </div>

      {llm && !llm.available && (
        <Card className="p-4 mb-4 text-sm text-amber-200 border-amber-500/30">
          No LLM is configured on the backend: {llm.error}
        </Card>
      )}

      <div className="flex-1 space-y-5 pb-4">
        {messages.length === 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-4">
            {STARTERS.map((s) => (
              <button key={s} onClick={() => send(s)} disabled={!llm?.available}
                      className="text-left rounded-xl border border-slate-800 bg-slate-900/50 px-4 py-3 text-sm text-slate-300 hover:border-brand-500/40 hover:text-white transition-colors disabled:opacity-40">
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-brand-500/20 border border-brand-500/25 px-4 py-2 text-sm text-slate-100 whitespace-pre-wrap">
                {m.content}
              </div>
            </div>
          ) : (
            <Card key={i} className="p-4"><AssistantMessage m={m} /></Card>
          ),
        )}
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-0 pt-2 pb-1 bg-slate-950">
        <div className="flex items-end gap-2 rounded-xl border border-slate-800 bg-slate-900 p-2 focus-within:border-brand-500/50">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
            }}
            rows={1}
            placeholder="Ask about growth, profitability, channels, a competitor… (Enter to send)"
            aria-label="Question"
            className="flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-slate-100 outline-none placeholder:text-slate-600 max-h-40"
          />
          {busy ? (
            <Button variant="ghost" onClick={() => abortRef.current?.abort()}>
              <AppIcon name="x" className="w-4 h-4" /> Stop
            </Button>
          ) : (
            <Button onClick={() => send()} disabled={!input.trim() || !llm?.available}>
              <AppIcon name="arrowRight" className="w-4 h-4" /> Send
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
