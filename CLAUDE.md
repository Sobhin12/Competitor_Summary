# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Competitor analysis and quarterly report generation for Indian health insurers — the
seven standalone health insurers (SAHI: Niva Bupa/NBHI, Star Health/STAR, Care
Health/CARE, ManipalCigna/CIGNA, Aditya Birla Health/ABHI, Narayana Health,
Galaxy Health) plus industry-wide GIC statistics. Two halves in one repo:

- `Backend/` — Python pipeline (retrieval → extraction → reporting) + FastAPI service
- `Frontend/` — Vite + React 19 + Tailwind v4 dashboard that drives the pipeline via the API

## Commands

All Backend commands run from `Backend/` using the project's own venv interpreter
(`myenv/Scripts/python.exe` — do not assume a globally-activated venv).

```bash
# Setup
myenv/Scripts/python.exe -m pip install -e ".[dev]"

# Run the full pipeline for one reporting period
python -m competitor_analysis.cli --fy FY25-26 --quarter Q3 --stage all

# Or split, with a human check of downloaded/extracted data in between
python -m competitor_analysis.cli --fy FY25-26 --quarter Q3 --stage download
python -m competitor_analysis.cli --fy FY25-26 --quarter Q3 --stage build

# Streamlit control dashboard (runs the CLI as a subprocess per stage)
streamlit run app/streamlit_app.py

# HTTP API backing the React dashboard
python -m uvicorn competitor_analysis.api.main:app --reload --port 8000
# interactive docs: http://localhost:8000/docs

# Tests
myenv/Scripts/python.exe -m pytest
myenv/Scripts/python.exe -m pytest tests/test_financial_year.py            # one file
myenv/Scripts/python.exe -m pytest tests/test_financial_year.py::test_name # one test
```

`Backend/.env` needs `PARALLEL_API_KEY` (Phase 1 retrieval) and `GEMINI_API_KEY`
(Phase 2 extraction).

Frontend (from `Frontend/`, two processes — start the backend first):

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api -> http://127.0.0.1:8000
npm run build       # production build -> dist/
npm run preview     # serve the production build locally
```

Override the proxy/base target if the backend runs elsewhere:
`VITE_API_TARGET=http://host:port npm run dev` (dev proxy) or
`VITE_API_BASE=http://host:port npm run build` (baked into a build).

## Architecture

### The pipeline is 4 phases, driven by a single reporting period

Everything is keyed off one FY+Quarter, set once via `config.set_period(fy, quarter)`
(or CLI `--fy`/`--quarter`). `config.py` derives the calendar mapping, download
directory, workbook column headers (`FY25-26_Q3`/`FY24-25_Q3`), and YTD divisors
from that — nothing hardcodes a date. A financial year is written as the span it
covers (`FY25-26` = Apr 2025–Mar 2026); `FY26` is accepted as a legacy alias and
normalized. Modules whose module-level constants derive from the active period
register via `config.on_period_change()` so a long-lived process (the API server)
picks up a later `set_period()` call instead of keeping the first run's period
baked in at import time — this has been a real bug source (see `pdf_cache`'s
company-PDF map and `data_engine`'s column headers), so a new period-derived
constant almost always needs this too.

1. **Phase 1 — Retrieval** (`ingestion/scraper.py`). A Parallel AI agent locates
   and downloads each insurer's IRDAI disclosure PDF (+ `GIC.xlsx`) into
   `data/downloads/{FY}/{Quarter}/`. Processors escalate `core` → `pro`, then a
   Search API fallback; successful URLs are cached as period-parameterised
   templates so later quarters often skip the agent. Never blocks Phase 2 — a
   missing insurer is skipped, not fatal.

2. **Phase 2 — Extraction** (`extraction/`), run by `pipeline.run_phase2()`.
   Fills `Data_Engine_UI.xlsx` ("Data Engine" sheet) from three independent
   sources merged onto the same rows:
   - `GIC.xlsx` → industry-wide rows (Slides 3–11, 14)
   - deterministic `pdfplumber` parse of each insurer's income-statement page
     (`pdf_cache.py`, `forms.py` → Slide 18)
   - Gemini-based extraction against IRDAI NL-form line items (`gemini.py`,
     batched/cached/rate-limited — 15 RPM free-tier quota is the binding
     constraint, not local parallelism — validated via Pydantic schemas in
     `schemas.py` → Slides 12–35)

   Every row is `(Slide #, Category, Company, Meric 1, Metric 2, <CUR_PERIOD>,
   <PRIOR_PERIOD>, Growth)`. Gemini responses are cached on disk keyed by
   company + source tables + prompt + model + period — a re-run is essentially
   free unless a prompt changed (`--no-cache`). PDF parsing is memory-bounded
   (`PDF_PARSE_MAX_WORKERS`) because Render's container OOMs above a few
   concurrent `pdfplumber` documents; one company's failure is isolated and
   never sinks the whole run.

3. **Phase 3 — Reporting** (`reporting/`). See below — this is the report/chart
   pipeline.

4. **Phase 4 — Dashboards.** `app/streamlit_app.py` (standalone control panel)
   and `Frontend/` (React dashboard talking to `api/`) both just drive the same
   CLI stages/API endpoints; neither contains pipeline logic of its own.

### Phase 3 in detail: Data Engine workbook → PDF report

```
Data_Engine_UI.xlsx  --[data.py]-->  pivoted per-slide dicts  --[report.py]-->  matplotlib pages  --[charts.py]-->  PDF
```

- **`reporting/data.py`** — flat access layer. `load_rows()` reads every
  populated row as a dict. `for_slide()`, `by_company()`, `pivot_metric1_only()`,
  `metric2_by_group()` reshape rows into `{company: {metric: (cur, prior)}}`
  shapes; `metric_series()` turns that into the final `(companies, prior_values,
  current_values)` triple a chart function consumes, in canonical
  `COMPANY_ORDER`, dropping companies with no data for that metric.
- **`reporting/theme.py`** — colors and `canonical_company()`, the fuzzy
  name-matcher that resolves any of the sheet's spelling variants ("Niva Bupa",
  "NBHI", a stray "Naryana" typo) down to one of the 7 canonical company keys,
  or `None` for aggregate labels ("Industry", "SAHI") that aren't an individual
  company.
- **`reporting/report.py`** — one `slide_NN(pdf, rows)` function per report page
  (3–35). Each pulls its slide's rows via `data.py`, decides whether there's
  anything to plot, computes rule-based "insight" bullets (leader/laggard +
  YoY delta via `leader_laggard_bullets`), and draws onto a `GridSpec` panel via
  `charts.py`. **Hard rule enforced throughout:** if a metric group has no
  current-period data at all, the section is skipped entirely — no placeholder
  box, no "not available" note. Every chart function returns `False` when
  there's nothing to plot; every section function checks that before allocating
  a panel. `build()` runs `SECTION_FUNCS` in order inside one `PdfPages` context.
- **`reporting/charts.py`** — pure matplotlib builders (`doughnut_pair`,
  `grouped_bar`, `single_bar`, `stacked_bar`, `change_bar`, `income_table`),
  each returning whether it drew anything. Includes automatic broken-axis
  handling (`_outlier_break`) when one company's value dwarfs the rest (e.g.
  young insurers' combined ratios — see `KNOWN_ISSUES.md` §2).

Triggered from the API by `api/runs.py::_phase_reporting()` →
`report.build(out_path, data_engine_path=run.data_engine_path)`. A run pauses at
`awaiting_report` after extraction so a human can review the workbook before
`POST /api/pipeline/{run_id}/continue-report` builds the PDF; `GET
/api/reports/{run_id}/download` serves it.

**A second reporting path exists but is not wired in**: `reporting/report_llm.py`
generates an HTML report via one Gemini call per Slide # (Chart.js configs
instead of matplotlib), with a `_verify_numbers()` pass that re-extracts every
number the model wrote and checks it traces back to a real Data Engine cell. It
imports `reporting.validate` (`extract_numbers`/`_flatten_numbers`/`_normalize`),
which does not exist in the repo — this module will raise `ImportError` if
invoked, and nothing in `runs.py`/`main.py`/`cli.py` calls it yet.

### Cross-cutting things worth knowing

- **Nothing resolves paths relative to cwd.** Every filesystem location comes
  from `paths.py`, so the pipeline runs identically from anywhere.
- **`data/` vs `artifacts/`.** `data/` is version-controlled input (downloads,
  the blank Data Engine template, logos, `source_links.json`). `artifacts/` is
  regenerable output (the working workbook, dated report PDFs, PDF-JSON/LLM
  caches) — never treat artifacts as a source of truth to hand-edit.
  `DATA_ENGINE_TEMPLATE` itself is never overwritten by a run; each run writes
  a fresh timestamped copy.
- **Prior-period facts** (`extraction/prior_facts.py`). A few PRIOR cells (slides 16/17/23-25/27/28)
  and the opening on-roll headcount aren't in the current filing; they come from last year's own
  filing, resolved per company and metric: last year's PDF (re-extracted, via
  `config.temporary_period`) -> `artifacts/cache/prior_facts/{FY}_{Q}.json` -> a validated old Data
  Engine workbook as last resort. Each completed company's CUR values are recorded to that JSON, which
  is what next year reads. `--no-prior-extract` skips the PDF step. Anything not found stays empty and
  is logged, never substituted.
- **Tests are period-agnostic by design.** Expected values are read live from
  the source filings at test time rather than hardcoded, so they keep testing
  logic rather than one quarter's numbers. A small number of tests
  deliberately assert against the current filings' column layouts — those say
  so in their own docstrings.
- **`KNOWN_ISSUES.md`** (`Backend/`) logs deliberately-unfixed findings and
  corrected past misdiagnoses in the extraction pipeline — read it before
  assuming an odd-looking extracted number (e.g. a >900% combined ratio for a
  startup insurer) is a bug rather than already-investigated real data.
- **`table_markdown.py`** (`extraction/`) has no caller and no test anywhere in
  the repo — confirmed dead code, not a path that's "wired in" to anything.
- **Frontend talks to the backend only through `src/api.js`.** It wraps
  `fetch`, unwraps FastAPI's `detail` error messages, and distinguishes
  "backend unreachable" (status 0) from a real HTTP error. Nothing in the UI
  simulates data — the company list is `source_links.json` and the activity
  feed is the pipeline's own log output, polled from `/api/pipeline/{id}/status`
  every 1.5s.
