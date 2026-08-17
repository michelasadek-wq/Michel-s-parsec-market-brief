# Open-source landscape — what exists, what we took, what we skipped

> Every major LLM-trading project on GitHub reviewed August 2026 (stars checked via GitHub API at review time).

| Project | Stars | State | Our take |
|---|---|---|---|
| TauricResearch/TradingAgents | 97.8k | active | Multi-agent bull/bear debate over trades. **Pattern reference only** — its published results do not survive out-of-sample testing (see evidence-review.md). |
| OpenBB | 71.8k | active | Open data platform. Good as a library; its quality news providers are all paid. Skipped. |
| virattt/ai-hedge-fund | 62.8k | active | Fundamentals agents; no news ingestion; requires a $200/mo data vendor. Skipped. |
| HKUDS/Vibe-Trading | 30.7k | very active | 24 data adapters, 9 backtest engines. **Took: data-adapter layering + scheduled-research pattern.** Full stack (FastAPI+React+Docker) far exceeds our need. |
| AI4Finance FinGPT | 21.1k | active | LoRA-tuned sentiment models needing a GPU. A frontier model already classifies better with zero infra. Skipped. |
| AI4Finance FinRobot | 7.8k | slowing | Notebook-grade platform. Research toy. Skipped. |
| ginlix-ai/LangAlpha | 1.6k | active | "Claude Code for markets" — **took: the morning-brief widget concept + compounding brief archive** (each day's brief informs the next). |
| daily-watchlist (Claude skill) | 57 | small | Exactly our shape in miniature — **took: report template ideas + source fallback chains.** |

**Conclusion:** the "RSS → model → chat brief" shape is a commodity. The useful
differentiation is portfolio-led U.S./global filtering, dynamic ticker feeds,
private IBKR P&L and exposure analysis, explicit data provenance, and a hard
separation between the monitoring brief and the transparent decision screen.
We kept those pieces and skipped the shared bet that model debate can reliably
pick trades from stale headlines.
