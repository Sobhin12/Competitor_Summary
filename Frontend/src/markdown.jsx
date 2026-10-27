// ---------------------------------------------------------------------------
// Minimal markdown for LLM output: headings, bullet/numbered lists, bold,
// italics, inline code, rules, paragraphs. Builds React elements (never
// innerHTML), so model output can't inject markup. Numbers the backend could
// not trace to a tool result are underlined in amber with an explanation.
// ---------------------------------------------------------------------------

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function flagUnverified(text, unverified, keyBase) {
  if (!unverified?.length) return [text];
  const re = new RegExp(`(${unverified.map(escapeRe).join("|")})`, "g");
  return text.split(re).map((part, i) =>
    unverified.includes(part) ? (
      <span key={`${keyBase}-u${i}`} title="Not traceable to the data - treat with caution"
            className="underline decoration-amber-400 decoration-dotted underline-offset-2 text-amber-200 cursor-help">
        {part}
      </span>
    ) : (
      part
    ),
  );
}

function inline(text, unverified, keyBase) {
  // **bold**, *italic* / _italic_, `code`
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;
  let last = 0;
  let m;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(...flagUnverified(text.slice(last, m.index), unverified, `${keyBase}-${i++}`));
    const tok = m[0];
    const k = `${keyBase}-t${i++}`;
    if (tok.startsWith("**")) out.push(<strong key={k} className="font-semibold text-white">{flagUnverified(tok.slice(2, -2), unverified, k)}</strong>);
    else if (tok.startsWith("`")) out.push(<code key={k} className="px-1 rounded bg-slate-800 text-[0.9em]">{tok.slice(1, -1)}</code>);
    else out.push(<em key={k}>{flagUnverified(tok.slice(1, -1), unverified, k)}</em>);
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(...flagUnverified(text.slice(last), unverified, `${keyBase}-end`));
  return out;
}

export function Markdown({ text, unverified = [], className = "" }) {
  const lines = (text || "").replace(/\r/g, "").split("\n");
  const blocks = [];
  let list = null;
  let para = [];

  const flushPara = () => {
    if (para.length) blocks.push({ kind: "p", text: para.join(" ") });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push(list);
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (!line.trim()) {
      flushPara();
      flushList();
    } else if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
      flushPara();
      flushList();
      blocks.push({ kind: "hr" });
    } else if (heading) {
      flushPara();
      flushList();
      blocks.push({ kind: "h", level: heading[1].length, text: heading[2] });
    } else if (bullet || numbered) {
      flushPara();
      const ordered = !!numbered;
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { kind: "list", ordered, items: [] };
      }
      list.items.push(bullet ? bullet[1] : numbered[2]);
    } else if (list && /^\s{2,}/.test(raw)) {
      list.items[list.items.length - 1] += " " + line.trim();
    } else {
      flushList();
      para.push(line.trim());
    }
  }
  flushPara();
  flushList();

  return (
    <div className={`text-sm leading-relaxed text-slate-200 space-y-2 ${className}`}>
      {blocks.map((b, i) => {
        const k = `b${i}`;
        if (b.kind === "hr") return <hr key={k} className="border-slate-800" />;
        if (b.kind === "h")
          return (
            <div key={k} className={`font-semibold text-white ${b.level <= 2 ? "text-base" : "text-sm"} pt-1`}>
              {inline(b.text, unverified, k)}
            </div>
          );
        if (b.kind === "list") {
          const Tag = b.ordered ? "ol" : "ul";
          return (
            <Tag key={k} className={`${b.ordered ? "list-decimal" : "list-disc"} pl-5 space-y-1 marker:text-slate-500`}>
              {b.items.map((it, j) => <li key={j}>{inline(it, unverified, `${k}-${j}`)}</li>)}
            </Tag>
          );
        }
        return <p key={k}>{inline(b.text, unverified, k)}</p>;
      })}
    </div>
  );
}
