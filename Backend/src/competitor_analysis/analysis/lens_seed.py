"""
Seed lenses for the "At a glance" page. Each is plain data in the format
lenses.validate_spec() accepts:

  id, title, theme   identity and grouping
  basis              'ytd' (default for everything) or 'quarter' (sequential analysis only)
  formula            per-company value, in the lens formula language (see lenses.py)
  unit               pct | pp | x | rs100 | cr   (how the value is displayed)
  higher_is_better   True / False / None (neutral)
  require            optional guard; a company failing it is left out (e.g. no premium growth)
  flag               optional per-company condition (companies the story is about)
  fires              when the lens is worth showing, using home/best/worst/median/rank/n/nflag
  impact             optional rupee value to NBHI in crore a year (annualise, home_cur available)
  so_what            sentence template written from NBHI's side ({home}, {best}, {best_name}, {nflag}...)
  priority, caveats  ranking weight and honest limits
"""

SEED_LENSES = [
    {
        "id": "growth_quality",
        "title": "Quality of growth: profit on each extra rupee of premium",
        "theme": "Growth",
        "basis": "ytd", "unit": "rs100", "higher_is_better": True,
        "formula": "chg('pbt') / chg('gwp') * 100",
        "require": "chg('gwp') > 0",
        "fires": "rank > n / 2",
        "so_what": "Each ₹100 of new premium moved NBHI's PBT by {home}; {best_name} moved it by {best}. "
                   "NBHI ranks {rank} of {n} on whether growth pays for itself.",
        "caveats": ["PBT includes investment income, which is not driven by new premium."],
        "priority": 1.6,
    },
    {
        "id": "cost_vs_premium",
        "title": "Operating leverage: health opex growth vs health premium growth",
        "theme": "Cost",
        "basis": "ytd", "unit": "pp", "higher_is_better": False,
        "formula": "gr('health_operating_expenses') - gr('health_gwp')",
        "fires": "home > 0 and rank > n / 2",
        "so_what": "NBHI's health opex grew {home} faster than health premium; the best in the peer set is "
                   "{best_name} at {best}. Costs are outrunning premium, which is a cost problem, not a pricing one.",
        "priority": 1.4,
    },
    {
        "id": "ri_dependence",
        "title": "Reinsurance-adjusted economics: share of gross commission handed back by reinsurers",
        "theme": "Cost",
        "basis": "ytd", "unit": "pct", "higher_is_better": None,
        "formula": "cur('health_ri_commission') / cur('health_gross_commission')",
        "fires": "worst > 1.5 * median",
        "so_what": "{worst_name} recovers {worst} of its gross health commission from reinsurers (peer median "
                   "{median}, NBHI {home}). Its low expense ratio is largely bought through treaties, so benchmark "
                   "against it with care.",
        "caveats": ["Reinsurance treaties can be repriced; the benefit is not structural."],
        "priority": 1.0,
    },
    {
        "id": "capital_runway",
        "title": "Capital runway: thin solvency headroom while growing fast and losing money",
        "theme": "Capital",
        "basis": "ytd", "unit": "x", "higher_is_better": True,
        "formula": "cur('solvency_ratio')",
        "flag": "cur('solvency_ratio') < 1.7 and gr('gwp') > 0.25 and chg('pbt') < 0",
        "fires": "nflag > 0",
        "so_what": "{flagged} combine solvency close to the 1.5x floor with fast growth and falling profit. "
                   "Expect a capital raise or slower growth within a few quarters, which is an opening for NBHI "
                   "(NBHI solvency {home}).",
        "caveats": ["Solvency can be lifted by a capital raise at any time; this flags pressure, not a date."],
        "priority": 1.5,
    },
    {
        "id": "earnings_source",
        "title": "Where earnings come from: health underwriting result vs investment income",
        "theme": "Earnings",
        "basis": "ytd", "unit": "x", "higher_is_better": True,
        "formula": "cur('health_uw_result') / cur('investment_income')",
        "fires": "home < 0",
        "so_what": "NBHI's health underwriting result is {home} its investment income "
                   "(negative: underwriting loss funded by investments); {best_name} is at {best}. "
                   "If yields soften, that gap hits NBHI first.",
        "caveats": ["Investment income is whole-company; underwriting is health only."],
        "priority": 1.2,
    },
    {
        "id": "share_shift",
        "title": "Share shifts: change in share of the seven insurers' GDPI",
        "theme": "Market",
        "basis": "ytd", "unit": "pp", "higher_is_better": True,
        "formula": "share('gdpi') - pshare('gdpi')",
        "flag": "share('gdpi') - pshare('gdpi') < -0.01",
        "fires": "nflag > 0 or abs(worst) > 0.01",
        "so_what": "Share moved by {home} for NBHI; the biggest gain is {best_name} at {best}. These are net "
                   "shifts side by side, not proof of who won whose customers.",
        "caveats": ["Net shifts only: the data cannot trace a customer between insurers."],
        "priority": 1.1,
    },
    {
        "id": "rupee_gap",
        "title": "Our gap to best-in-class, priced in rupees: health combined ratio",
        "theme": "Profitability",
        "basis": "ytd", "unit": "pct", "higher_is_better": False,
        "formula": "cur('health_combined_ratio')",
        "fires": "home > best",
        "impact": "(home - best) * home_cur('health_earned_premium') * annualise",
        "so_what": "NBHI's health combined ratio is {home} against {best} at {best_name}. Closing the gap is worth "
                   "about {impact} a year on today's earned premium, more than most growth initiatives.",
        "caveats": ["Assumes the gap closes at constant earned premium; the annual figure scales the year to date."],
        "priority": 2.0,
    },
    {
        "id": "sequential_loss_ratio",
        "title": "Quarter to quarter: health loss ratio, this quarter vs the one before",
        "theme": "Profitability",
        "basis": "quarter", "unit": "pct", "higher_is_better": False,
        "formula": "cur('health_claims') / cur('health_earned_premium')",
        "show_prior": "prior('health_claims') / prior('health_earned_premium')",
        "flag": "cur('health_claims') / cur('health_earned_premium') - prior('health_claims') / prior('health_earned_premium') > 0.02",
        "fires": "home > median and nflag > 0",
        "so_what": "On a single-quarter basis NBHI's health loss ratio is {home} (peer median {median}); the best "
                   "is {best_name} at {best}. Loss ratio worsened by more than 2 pp on the previous quarter at: {flagged}.",
        "caveats": ["Single-quarter figures are seasonal; compare with the year-to-date view before concluding."],
        "priority": 1.0,
    },
]
