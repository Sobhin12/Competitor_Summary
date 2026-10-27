"""
Interactive analysis layer (v2): a normalised fact store over every Data
Engine workbook plus Historical_Trends.xlsx, deterministic analysis tools on
top of it, rule-based insights, and an LLM chat agent that only ever states
numbers those tools produced.

Nothing here touches the global reporting period (config.set_period) - every
fact carries its own period, so the store can hold many quarters at once.
"""
