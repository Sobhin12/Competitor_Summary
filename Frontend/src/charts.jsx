import {
  ResponsiveContainer, BarChart, Bar, LineChart, Line, PieChart, Pie, Cell, XAxis, YAxis, Tooltip,
  CartesianGrid, Legend, ReferenceLine, LabelList,
} from "recharts";

// ---------------------------------------------------------------------------
// Chart rendering for the analysis views. Every chart - from the chat agent,
// a highlight card or the deep-dive - arrives as the same small spec:
//   { type, title, unit, categories[], series[{name, values[]}], start?, end?, note? }
// Units: pct values are fractions (0.253 -> 25.3%); pp are points; cr is INR crore.
// ---------------------------------------------------------------------------

// Fixed categorical order, validated for CVD/contrast on the slate-900 surface
// (dataviz validate_palette --mode dark). Colour follows the company, never its
// rank, so a filter can't repaint the survivors.
export const COMPANY_COLORS = {
  NBHI: "#3987e5", STAR: "#d95926", CARE: "#199e70", CIGNA: "#c98500",
  ABHI: "#d55181", NARAYANA: "#008300", GALAXY: "#9085e9",
  SAHI: "#94a3b8", Industry: "#cbd5e1",
};
export const COMPANY_NAMES = {
  NBHI: "Niva Bupa", STAR: "Star Health", CARE: "Care Health", CIGNA: "ManipalCigna",
  ABHI: "Aditya Birla", NARAYANA: "Narayana", GALAXY: "Galaxy",
};
const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
const PRIOR = "#64748b";
const UP = "#199e70";
const DOWN = "#e66767";
const AXIS = "#64748b";
const GRID = "#1e293b";

export function fmtValue(v, unit) {
  if (v == null || Number.isNaN(v)) return "–";
  switch (unit) {
    case "pct": return `${(v * 100).toFixed(1)}%`;
    case "pp": return `${v > 0 ? "+" : ""}${v.toFixed(1)} pp`;
    case "cr": return `₹${Math.abs(v) >= 100 ? v.toLocaleString("en-IN", { maximumFractionDigits: 0 }) : v.toFixed(2)} Cr`;
    case "lakh": return `₹${v.toFixed(2)} L`;
    case "inr": return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
    case "x": return `${v.toFixed(2)}x`;
    case "count": return v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
    default: return v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }
}

function axisFmt(unit) {
  return (v) => {
    if (unit === "pct") return `${Math.round(v * 100)}%`;
    if (unit === "pp") return `${v}`;
    if (unit === "x") return `${v}x`;
    if (Math.abs(v) >= 1000) return `${(v / 1000).toLocaleString("en-IN", { maximumFractionDigits: 1 })}k`;
    return `${v}`;
  };
}

const label = (c) => COMPANY_NAMES[c] || c;
const colorFor = (name, i) => COMPANY_COLORS[name] || SERIES[i % SERIES.length];

function ChartTooltip({ active, payload, label: l, unit }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900/95 px-3 py-2 text-xs shadow-lg">
      <div className="font-medium text-slate-200 mb-1">{label(l)}</div>
      {payload.filter((p) => p.dataKey !== "__base").map((p) => (
        <div key={p.dataKey} className="flex items-center gap-2 text-slate-300">
          <span className="w-2 h-2 rounded-sm" style={{ background: p.color || p.payload?.fill }} />
          <span className="text-slate-400">{p.name}</span>
          <span className="ml-auto tabular-nums text-slate-100">{fmtValue(p.value, unit)}</span>
        </div>
      ))}
    </div>
  );
}

// Long category names (channels, states) are angled and clipped to fit; a
// series of short labels (FY18..FY26) instead thins its ticks rather than
// colliding. The tooltip always carries the full name.
const tickLabel = (c) => {
  const s = label(c);
  return s.length > 16 ? `${s.slice(0, 15)}…` : s;
};

const commonAxes = (unit, angled, thin = false) => [
  <CartesianGrid key="g" stroke={GRID} vertical={false} />,
  <XAxis key="x" dataKey="category" tickFormatter={tickLabel} tick={{ fill: AXIS, fontSize: 11 }}
         axisLine={{ stroke: GRID }} tickLine={false} interval={thin ? "preserveStartEnd" : 0} minTickGap={6}
         angle={angled ? -30 : 0} textAnchor={angled ? "end" : "middle"} height={angled ? 72 : 28} />,
  <YAxis key="y" tickFormatter={axisFmt(unit)} tick={{ fill: AXIS, fontSize: 11 }} axisLine={false}
         tickLine={false} width={52} />,
];

/** Bar/line data rows: one per category, one key per series. */
function rows(spec) {
  return spec.categories.map((c, i) => {
    const r = { category: c };
    spec.series.forEach((s) => { r[s.name] = s.values[i]; });
    return r;
  });
}

function Waterfall({ spec }) {
  // Floating bars: an invisible base plus the visible step. With start/end
  // (a bridge, e.g. PBT last year -> this year) those totals bracket the steps.
  const steps = spec.series[0].values;
  const data = [];
  let run = 0;
  if (spec.start) {
    data.push({ category: spec.start.label, __base: 0, value: spec.start.value, kind: "total" });
    run = spec.start.value;
  }
  spec.categories.forEach((c, i) => {
    const v = steps[i] ?? 0;
    const lo = Math.min(run, run + v);
    data.push({ category: c, __base: lo, value: Math.abs(v), signed: v, kind: v >= 0 ? "up" : "down" });
    run += v;
  });
  if (spec.end) data.push({ category: spec.end.label, __base: 0, value: spec.end.value, kind: "total" });
  const negTotals = data.some((d) => d.kind === "total" && d.value < 0);
  // Negative totals (a loss) draw downward from zero: base = value, height = |value|.
  if (negTotals) data.forEach((d) => { if (d.kind === "total" && d.value < 0) { d.__base = d.value; d.signed = d.value; d.value = -d.value; } });
  const fill = (d) => (d.kind === "total" ? "#475569" : d.kind === "up" ? UP : DOWN);
  return (
    <BarChart data={data} margin={{ top: 18, right: 8, left: 0, bottom: 0 }}>
      {commonAxes(spec.unit, data.length > 5)}
      <ReferenceLine y={0} stroke="#334155" />
      <Tooltip cursor={{ fill: "rgba(148,163,184,0.06)" }}
               content={({ active, payload }) => active && payload?.length ? (
                 <div className="rounded-lg border border-slate-700 bg-slate-900/95 px-3 py-2 text-xs">
                   <div className="text-slate-200 font-medium">{payload[0].payload.category}</div>
                   <div className="text-slate-300 tabular-nums">
                     {fmtValue(payload[0].payload.signed ?? payload[0].payload.value, spec.unit)}
                   </div>
                 </div>) : null} />
      <Bar dataKey="__base" stackId="w" fill="transparent" isAnimationActive={false} />
      <Bar dataKey="value" stackId="w" radius={[4, 4, 4, 4]} maxBarSize={42}>
        {data.map((d, i) => <Cell key={i} fill={fill(d)} />)}
        <LabelList dataKey="signed" position="top" formatter={(v) => (v == null ? "" : fmtValue(v, spec.unit))}
                   style={{ fill: "#cbd5e1", fontSize: 10 }} />
      </Bar>
    </BarChart>
  );
}

export function ChartBlock({ spec, height = 260, compact = false }) {
  if (!spec?.categories?.length || !spec?.series?.length) return null;
  const data = rows(spec);
  const unit = spec.unit;
  const multi = spec.series.length > 1;
  const maxLen = Math.max(...spec.categories.map((c) => label(c).length));
  const longLabels = maxLen > 9 || (spec.type !== "line" && spec.categories.length > 8);
  let chart;

  if (spec.type === "waterfall") {
    chart = <Waterfall spec={spec} />;
  } else if (spec.type === "line") {
    chart = (
      <LineChart data={data} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
        {commonAxes(unit, longLabels, true)}
        <Tooltip content={<ChartTooltip unit={unit} />} cursor={{ stroke: "#334155" }} />
        {multi && <Legend formatter={(v) => <span className="text-slate-300 text-xs">{label(v)}</span>} iconSize={8} />}
        {spec.series.map((s, i) => (
          <Line key={s.name} type="monotone" dataKey={s.name} name={label(s.name)} stroke={s.color || colorFor(s.name, i)}
                strokeWidth={2} strokeDasharray={s.dashed ? "4 3" : undefined}
                dot={{ r: 3, strokeWidth: 0, fill: s.color || colorFor(s.name, i) }} connectNulls />
        ))}
      </LineChart>
    );
  } else if (spec.type === "doughnut") {
    const s = spec.series[0];
    const pie = spec.categories.map((c, i) => ({ name: c, value: s.values[i] })).filter((d) => d.value > 0);
    chart = (
      <PieChart>
        <Tooltip content={({ active, payload }) => active && payload?.length ? (
          <div className="rounded-lg border border-slate-700 bg-slate-900/95 px-3 py-2 text-xs text-slate-200">
            {label(payload[0].name)}: {fmtValue(payload[0].value, unit)}
          </div>) : null} />
        <Legend formatter={(v) => <span className="text-slate-300 text-xs">{label(v)}</span>} iconSize={8} />
        <Pie data={pie} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="85%" paddingAngle={1}
             stroke="#0f172a" strokeWidth={2}>
          {pie.map((d, i) => <Cell key={d.name} fill={colorFor(d.name, i)} />)}
        </Pie>
      </PieChart>
    );
  } else {
    // bar, grouped_bar, stacked_bar
    const stacked = spec.type === "stacked_bar";
    const single = !multi;
    chart = (
      <BarChart data={data} margin={{ top: single ? 18 : 10, right: 8, left: longLabels ? 28 : 0, bottom: 0 }} barGap={2}>
        {commonAxes(unit, longLabels)}
        <ReferenceLine y={0} stroke="#334155" />
        <Tooltip content={<ChartTooltip unit={unit} />} cursor={{ fill: "rgba(148,163,184,0.06)" }} />
        {multi && <Legend formatter={(v) => <span className="text-slate-300 text-xs">{label(v)}</span>} iconSize={8} />}
        {spec.series.map((s, i) => {
          // Two-period comparisons (prior vs current) read best as muted vs. accent.
          const isPrior = multi && spec.series.length === 2 && i === 0 && !COMPANY_COLORS[s.name];
          return (
            <Bar key={s.name} dataKey={s.name} name={label(s.name)} stackId={stacked ? "s" : undefined}
                 fill={s.color || (isPrior ? PRIOR : colorFor(s.name, multi ? i : 0))} radius={stacked ? 0 : [4, 4, 0, 0]}
                 maxBarSize={single ? 40 : 28} stroke={stacked ? "#0f172a" : undefined} strokeWidth={stacked ? 1 : 0}>
              {single && data.map((d, j) => (
                <Cell key={j} fill={COMPANY_COLORS[d.category] || (d[s.name] < 0 && unit === "pp" ? DOWN : SERIES[0])} />
              ))}
              {single && !compact && (
                <LabelList dataKey={s.name} position="top" formatter={(v) => fmtValue(v, unit)}
                           style={{ fill: "#cbd5e1", fontSize: 10 }} />
              )}
            </Bar>
          );
        })}
      </BarChart>
    );
  }

  return (
    <figure className="w-full">
      {spec.title && !compact && <figcaption className="text-xs font-medium text-slate-300 mb-2">{spec.title}</figcaption>}
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">{chart}</ResponsiveContainer>
      </div>
      {spec.note && <p className="text-[11px] text-slate-500 mt-1.5">{spec.note}</p>}
    </figure>
  );
}

export function TableBlock({ spec }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      {spec.title && <div className="px-3 py-2 text-xs font-medium text-slate-300 border-b border-slate-800">{spec.title}</div>}
      <table className="w-full text-xs">
        <thead>
          <tr className="text-slate-400">
            {spec.columns.map((c) => <th key={c} className="text-left font-medium px-3 py-1.5">{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {spec.rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-800/70 text-slate-200">
              {r.map((cell, j) => (
                <td key={j} className={`px-3 py-1.5 ${typeof cell === "number" ? "tabular-nums" : ""}`}>
                  {cell == null ? "–" : typeof cell === "number" ? cell.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : COMPANY_NAMES[cell] || cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
