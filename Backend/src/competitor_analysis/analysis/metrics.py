"""
Metric registry: maps the Data Engine's raw (Category, Company, Meric 1,
Metric 2) labelling onto stable metric ids with a unit and a direction.

Slide numbers are deliberately NOT part of a metric's identity - they shift
between template versions (the FY26-27 Q1 workbook numbers its Historical
Trends block 30-34, the template 31-35). Category + Meric 1 text is what
stays put, so that is what this module keys on.

Units after normalisation (see MetricSpec.scale):
  cr     - INR crore           pct   - fraction (0.25 == 25%)
  lakh   - INR lakh            x     - multiple (solvency 1.8x)
  inr    - INR                 count - plain count
  raw    - unknown/as filed (an unrecognised metric)
"""
import re
from dataclasses import dataclass, field

from competitor_analysis.reporting import theme


@dataclass(frozen=True)
class MetricSpec:
    id: str
    label: str
    section: str
    unit: str
    # True: higher is better, False: lower is better, None: neutral (a mix share).
    higher_is_better: bool | None = True
    # Multiplier applied to the raw Data Engine cell to reach `unit` (the
    # workbook keeps some cells in lakhs/plain rupees, see report.py).
    scale: float = 1.0
    # What the dimension means when a metric is split (segment, channel...).
    dimension: str | None = None
    description: str = ""
    aliases: tuple = field(default_factory=tuple)
    # Headline weight when ranking moves (tools.top_movers): <1 for lines
    # that are small, derivative, or flagged best-effort in the PDF report.
    weight: float = 1.0


SECTIONS = ["Market", "Revenue & Growth", "Segment & Channel", "Geography", "Income Statement",
            "Segment P&L - Health", "Segment P&L - PA", "Segment P&L - Travel", "Ratios & Efficiency",
            "Claims & Service", "Capital", "Investments", "Distribution", "Other"]

_BASE = [
    # Market
    MetricSpec("gdpi_by_lob", "GDPI by line of business", "Market", "cr", None, dimension="Line of business",
               description="Gross direct premium by line of business, for the industry and its segments "
                           "(Public GI, Private GI, SAHI, Health industry)."),
    MetricSpec("segment_gdpi", "Segment-wise GDPI", "Market", "cr", None, dimension="Segment",
               description="Health GDPI split Govt./Group/PA/Retail/Travel for Industry, Public GI, Private GI, SAHI."),
    # Revenue & growth
    MetricSpec("gdpi", "GDPI (Health + PA + Travel)", "Revenue & Growth", "cr",
               description="Gross direct premium income, year to date.", aliases=("revenue", "premium", "topline")),
    MetricSpec("gdpi_growth", "GDPI growth (YoY)", "Revenue & Growth", "pct", weight=0.6,
               description="Year-on-year growth of GDPI (same period last year)."),
    MetricSpec("gdpi_cagr", "GDPI CAGR", "Revenue & Growth", "pct"),
    MetricSpec("retail_revenue", "Retail health revenue", "Revenue & Growth", "cr"),
    MetricSpec("retail_accretion", "Retail revenue accretion", "Revenue & Growth", "cr",
               description="Retail revenue this period minus the same period last year."),
    MetricSpec("individual_ats", "Individual agent ATS", "Revenue & Growth", "inr",
               description="Average ticket size: individual-agent GWP / individual-agent policies."),
    MetricSpec("avg_productivity", "Average productivity per agent", "Revenue & Growth", "lakh",
               description="Premium per individual agent (year to date, not annualised)."),
    # Segment & channel
    MetricSpec("segment_mix", "Segment mix (% of GDPI)", "Segment & Channel", "pct", None, dimension="Segment"),
    MetricSpec("channel_mix", "Channel mix (% of GDPI)", "Segment & Channel", "pct", None, dimension="Channel"),
    MetricSpec("channel_commission", "Gross commission by channel", "Segment & Channel", "cr", None,
               scale=0.01, dimension="Channel"),
    MetricSpec("channel_commission_pct", "Commission % of channel premium", "Segment & Channel", "pct", None,
               dimension="Channel",
               description="Each channel's commission as a share of that channel's own premium."),
    # Geography
    MetricSpec("zone_mix", "Zone mix (% of GDPI)", "Geography", "pct", None, dimension="Zone"),
    MetricSpec("state_mix", "State mix (% of GDPI)", "Geography", "pct", None, dimension="State"),
    # Income statement
    MetricSpec("gwp", "Gross written premium (GWP)", "Income Statement", "cr"),
    MetricSpec("nwp", "Net written premium (NWP)", "Income Statement", "cr"),
    MetricSpec("earned_premium", "Earned premium", "Income Statement", "cr"),
    MetricSpec("investment_income", "Investment income", "Income Statement", "cr"),
    MetricSpec("claims", "Claims incurred", "Income Statement", "cr", False),
    MetricSpec("total_overheads", "Total overheads", "Income Statement", "cr", False),
    MetricSpec("pbt", "Profit before tax (PBT)", "Income Statement", "cr", aliases=("profit",)),
    MetricSpec("pat", "Profit after tax (PAT)", "Income Statement", "cr"),
    # Ratios & efficiency
    MetricSpec("loss_ratio", "Loss ratio", "Ratios & Efficiency", "pct", False,
               description="Claims incurred / earned premium.", aliases=("claims ratio", "lr")),
    MetricSpec("expense_ratio", "Expense ratio", "Ratios & Efficiency", "pct", False),
    MetricSpec("combined_ratio", "Combined ratio", "Ratios & Efficiency", "pct", False,
               description="Loss ratio + expense ratio. Above 100% means an underwriting loss.",
               aliases=("cor", "cr")),
    MetricSpec("expense_of_mgmt_ratio", "Expense of management ratio", "Ratios & Efficiency", "pct", False),
    MetricSpec("opex_to_gwp", "Opex to GWP", "Ratios & Efficiency", "pct", False),
    MetricSpec("manpower_to_gwp", "Manpower cost to GWP", "Ratios & Efficiency", "pct", False),
    MetricSpec("it_to_gwp", "IT spend to GWP", "Ratios & Efficiency", "pct", None),
    MetricSpec("manpower_to_opex", "Manpower cost to total opex", "Ratios & Efficiency", "pct", None),
    MetricSpec("manpower_cost_per_employee", "Manpower cost per employee", "Ratios & Efficiency", "lakh", None),
    MetricSpec("facility_rental_per_office", "Facility rental per office per month", "Ratios & Efficiency",
               "lakh", False),
    MetricSpec("ri_ceding_ratio", "RI ceding to GWP", "Ratios & Efficiency", "pct", None),
    MetricSpec("ri_commission_ratio", "RI commission to RI ceding", "Ratios & Efficiency", "pct", True),
    # Claims & service
    MetricSpec("claim_settlement_ratio", "Claims settlement ratio", "Claims & Service", "pct"),
    MetricSpec("claim_settlement_ratio_amount", "Claims settlement ratio (amount)", "Claims & Service", "pct"),
    MetricSpec("avg_claim_size", "Average claim size", "Claims & Service", "lakh", None, scale=1e-5,
               description="Best-effort estimate.", weight=0.5),
    MetricSpec("claims_to_policies", "No. of claims to no. of policies", "Claims & Service", "pct", False,
               description="Best-effort estimate.", weight=0.5),
    MetricSpec("claim_complaints_per_10k", "Claim complaints per 10,000 claims", "Claims & Service", "count", False),
    MetricSpec("policy_complaints_per_10k", "Policy complaints per 10,000 policies", "Claims & Service", "count",
               False),
    MetricSpec("total_claims", "Total no. of claims", "Claims & Service", "count", None),
    MetricSpec("total_policies", "Total no. of policies", "Claims & Service", "count", True),
    # Capital
    MetricSpec("capital", "Capital", "Capital", "cr", None),
    MetricSpec("net_worth", "Net worth", "Capital", "cr", scale=0.01),
    MetricSpec("roe", "Return on equity (ROE)", "Capital", "pct", description="PAT / average net worth."),
    MetricSpec("solvency_ratio", "Solvency ratio", "Capital", "x",
               description="Regulatory minimum is 1.5x."),
    # Investments
    MetricSpec("aum", "AUM (overall)", "Investments", "cr"),
    MetricSpec("aum_policyholders", "AUM - policyholders", "Investments", "cr"),
    MetricSpec("aum_shareholders", "AUM - shareholders", "Investments", "cr"),
    MetricSpec("investment_yield", "Investment yield", "Investments", "pct",
               description="Year-to-date yield (not annualised for Q1-Q3)."),
    MetricSpec("investment_portfolio", "Investment portfolio by asset class", "Investments", "cr", None,
               dimension="Asset class"),
    MetricSpec("debt_rating_mix", "Debt portfolio by rating", "Investments", "pct", None, dimension="Rating"),
    MetricSpec("debt_maturity_mix", "Debt portfolio by maturity", "Investments", "pct", None, dimension="Maturity"),
    # Distribution
    MetricSpec("agents", "Individual agents", "Distribution", "count"),
    MetricSpec("employees", "On-roll employees", "Distribution", "count", None),
    MetricSpec("intermediaries", "Intermediaries", "Distribution", "count", None, dimension="Intermediary type"),
    MetricSpec("offices", "No. of offices", "Distribution", "count", None),
]

# Segment P&L lines, repeated for Health / PA / Travel under a prefix.
_SEGMENT_LINES = [
    ("gwp", "GWP", "cr", True), ("nwp", "NWP", "cr", True), ("earned_premium", "earned premium", "cr", True),
    ("claims", "claims", "cr", False), ("gross_commission", "gross commission", "cr", False),
    ("net_commission", "net commission", "cr", False), ("ri_commission", "RI commission", "cr", None),
    ("operating_expenses", "operating expenses", "cr", False),
    ("uw_result", "underwriting profit/(loss)", "cr", True),
    ("loss_ratio", "loss ratio", "pct", False), ("expense_ratio", "expense ratio", "pct", False),
    ("combined_ratio", "combined ratio", "pct", False),
]
SEGMENT_PL = {"Health": "health", "Personal Accident": "pa", "Travel": "travel"}
_SEGMENT_SECTION = {"health": "Segment P&L - Health", "pa": "Segment P&L - PA", "travel": "Segment P&L - Travel"}
_SEGMENT_NAME = {"health": "Health", "pa": "PA", "travel": "Travel"}

for _prefix in SEGMENT_PL.values():
    for _id, _label, _unit, _hib in _SEGMENT_LINES:
        _BASE.append(MetricSpec(f"{_prefix}_{_id}", f"{_SEGMENT_NAME[_prefix]} {_label}",
                                _SEGMENT_SECTION[_prefix], _unit, _hib,
                                weight={"health": 0.8, "pa": 0.35, "travel": 0.35}[_prefix]))

METRICS: dict[str, MetricSpec] = {m.id: m for m in _BASE}

# Raw Meric 1 text (lowercased, whitespace-collapsed) -> metric id, for rows
# where the Company column carries the entity.
_RAW_M1 = {
    "revenue growth (gdpi)": "gdpi",
    "growth %": "gdpi_growth", "sahi growth": "gdpi_growth", "gdpi growth sahi": "gdpi_growth",
    "cagr": "gdpi_cagr",
    "retail revenue": "retail_revenue", "retail accretion": "retail_accretion",
    "individual ats": "individual_ats", "average productivity (per agent)": "avg_productivity",
    "channel-wise gross commision % to gdpi": "channel_commission",
    "channel-wise gross commission % to gdpi": "channel_commission",
    "channel-wise commission % to channel premium": "channel_commission_pct",
    "total no. of claims (nl-45)": "total_claims", "total no. of policies (nl-36)": "total_policies",
    "claim complaints per 10,000 claims": "claim_complaints_per_10k",
    "policy complaints per 10,000 policies": "policy_complaints_per_10k",
    "gross written premium": "gwp", "gwp": "gwp", "net written premium": "nwp",
    "earned premium": "earned_premium", "investment income": "investment_income", "claims": "claims",
    "total overheads": "total_overheads", "pbt": "pbt", "pat": "pat",
    "loss ratio": "loss_ratio", "expense ratio": "expense_ratio", "combined ratio": "combined_ratio",
    "expense of management ratio": "expense_of_mgmt_ratio",
    "opex. to gwp ratio": "opex_to_gwp", "manpower to gwp ratio": "manpower_to_gwp",
    "it spend to gwp ratio": "it_to_gwp", "manpower cost to total opex": "manpower_to_opex",
    "manpower cost per employee": "manpower_cost_per_employee",
    "facility rental per office per month": "facility_rental_per_office",
    "ri ceding to gwp ratio": "ri_ceding_ratio", "ri commission to ri ceding": "ri_commission_ratio",
    "claims settlement ratio": "claim_settlement_ratio", "claim settlement ratio": "claim_settlement_ratio",
    "claim settlement ratio (amount)": "claim_settlement_ratio_amount",
    "claims settlement ratio (amount)": "claim_settlement_ratio_amount",
    "average claim size": "avg_claim_size", "no. of claims to no. of policies": "claims_to_policies",
    "capital": "capital", "net worth": "net_worth", "roe (sahi)": "roe", "roe": "roe",
    "solvency ratios": "solvency_ratio", "solvency ratio": "solvency_ratio",
    "aum (overall)": "aum", "aum -policyholders": "aum_policyholders", "aum -shareholders": "aum_shareholders",
    "investment yield": "investment_yield",
    "agents": "agents", "employees": "employees", "intermediaries": "intermediaries",
    "no. of offices": "offices",
}
_RAW_SEGMENT_LINE = {
    "gross written premium": "gwp", "net written premium": "nwp", "earned premium": "earned_premium",
    "claims": "claims", "gross commission": "gross_commission", "net commission": "net_commission",
    "ri commission": "ri_commission", "operating expenses": "operating_expenses",
    "uw profit/(loss)": "uw_result", "loss ratio": "loss_ratio", "expense ratio": "expense_ratio",
    "combined ratio": "combined_ratio",
}
# Metrics whose Metric 2 is a real split (a channel/intermediary type) rather
# than a per-row caption ("Avg. Maturity (4.6 years)", "On-roll Employee").
_M2_IS_DIMENSION = {"channel_commission", "channel_commission_pct", "intermediaries"}

# Historical_Trends.xlsx table title (lowercased) -> metric id.
HISTORICAL_TABLES = {
    "gdpi": "gdpi", "gdpi growth": "gdpi_growth", "retail health": "retail_revenue",
    "retail health accretion": "retail_accretion", "ats": "individual_ats",
    "average productivity (per agent)": "avg_productivity", "gwp": "gwp", "pbt": "pbt",
    "loss ratio": "loss_ratio", "expense ratio": "expense_ratio", "combined ratio": "combined_ratio",
    "expense of management ratio": "expense_of_mgmt_ratio", "ri ceding ratio": "ri_ceding_ratio",
    "ri commission to ceding ratio": "ri_commission_ratio", "roe": "roe", "solvency ratio": "solvency_ratio",
    "aum": "aum", "aum shareholders": "aum_shareholders", "aum policyholders": "aum_policyholders",
    "investment yield": "investment_yield", "employees": "employees", "agents": "agents", "offices": "offices",
}

ZONES = {"north", "south", "east", "west", "central"}

# Non-company entity labels -> canonical aggregate key.
_AGGREGATES = {
    "sahi": "SAHI", "sahi market": "SAHI", "sahi's": "SAHI", "stand-alone health sub total": "SAHI",
    "industry": "Industry", "industry total": "Industry",
    "public gi": "Public GI", "pvt. gi": "Private GI", "pvt gi": "Private GI", "private gi": "Private GI",
    "health industry (inc. pa and travel)": "Health Industry", "health industry": "Health Industry",
}
COMPANY_KEYS = ["NBHI", "STAR", "CARE", "CIGNA", "ABHI", "NARAYANA", "GALAXY"]
COMPANY_NAMES = {
    "NBHI": "Niva Bupa", "STAR": "Star Health", "CARE": "Care Health", "CIGNA": "ManipalCigna",
    "ABHI": "Aditya Birla Health", "NARAYANA": "Narayana Health", "GALAXY": "Galaxy Health",
}


def _clean(s):
    return re.sub(r"\s+", " ", str(s)).strip() if s is not None else None


def entity_key(raw):
    """Canonical company key, a known aggregate key, or None."""
    if raw is None:
        return None
    key = theme.canonical_company(raw)
    if key:
        return key
    return _AGGREGATES.get(_clean(raw).lower())


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def _register_unknown(category, m1):
    """A Meric 1 this registry doesn't know yet still becomes queryable, under
    an auto id with unit 'raw' - a new template row is never silently dropped."""
    mid = _slug(f"{category}_{m1}")
    if mid not in METRICS:
        METRICS[mid] = MetricSpec(mid, _clean(m1), "Other", "raw", None, description=f"Unmapped row from '{category}'.")
    return mid


def classify(category, company, m1, m2):
    """(metric_id, dimension, entity) for one Data Engine row, or None when
    the row duplicates data held elsewhere (Slide 5's per-company GDPI) or
    carries no identifiable entity."""
    cat = _clean(category) or ""
    cat_l = cat.lower()
    m1c, m2c = _clean(m1), _clean(m2)
    if not m1c:
        return None

    if cat_l.startswith("overall industry"):
        # Market-share block: Company is the market (Industry / Health
        # Industry / Public GI / Pvt GI / SAHI Market / a company) and
        # Metric 2 the line of business. Slide 5 instead puts a company name
        # in Metric 2 - that's just GDPI again, already held as `gdpi`.
        if theme.canonical_company(m2c):
            return None
        return "gdpi_by_lob", m2c, entity_key(company) or _clean(company)

    m1_entity = entity_key(m1c)
    if m1_entity:
        metric = {"segment wise gdpi": "segment_gdpi", "segment wise gdpi - health": "segment_mix",
                  "gdpi by channel": "channel_mix"}.get(cat_l) or _register_unknown(cat, cat)
        return metric, m2c, m1_entity

    entity = entity_key(company)
    if entity is None:
        return None

    if cat_l.startswith("segment p&l"):
        seg = next((p for name, p in SEGMENT_PL.items() if name.lower() in cat_l), None)
        line = _RAW_SEGMENT_LINE.get(m1c.lower())
        if seg and line:
            return f"{seg}_{line}", None, entity
    if cat_l == "investment portfolio":
        return "investment_portfolio", m1c, entity
    if cat_l == "debt portfolio":
        return ("debt_maturity_mix" if "year" in m1c.lower() else "debt_rating_mix"), m1c, entity
    if cat_l.startswith("revenue (segment"):
        if m1c.lower() in ZONES:
            return "zone_mix", m1c, entity
        if m2c is None and m1c.lower() not in _RAW_M1:
            return "state_mix", m1c, entity

    metric = _RAW_M1.get(m1c.lower()) or _register_unknown(cat, m1c)
    return metric, (m2c if metric in _M2_IS_DIMENSION else None), entity


def find_metrics(query):
    """Registry entries whose id/label/aliases/section match every word of `query`."""
    words = [w for w in re.split(r"\W+", (query or "").lower()) if w]
    out = []
    for m in METRICS.values():
        hay = " ".join([m.id.replace("_", " "), m.label.lower(), m.section.lower(), " ".join(m.aliases),
                        (m.dimension or "").lower()])
        if all(w in hay for w in words):
            out.append(m)
    return out
