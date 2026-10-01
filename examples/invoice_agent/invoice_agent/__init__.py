"""Example 2: a synthetic invoice-operations agent built with LangGraph.

All data is invented ("Acme Ledger" customers and invoices). The agent's
planner is scripted Python, not an LLM: the point is a real LangGraph
tool-calling loop whose trajectory AgentForge can record and check, with a
v2 that carries deliberate regressions. See README "Trajectory evaluation".
"""
