# Evidence review — does news trading make money, and should models debate?

> Why this product briefs and refuses to signal. Reviewed August 2026.

## Question 1: does LLM news-trading give retail an edge?

**No — and the product's no-signals guardrail is built on that finding.**

- Lopez-Lira & Tang's famous headline result (93% direction accuracy, ~700% gross) requires ~190% daily turnover and **goes unprofitable at 20 bps of trading costs**; the edge had already decayed ~80% by early 2024.
- "Profit Mirage" (arXiv 2510.07920) back-tested the popular agent frameworks (FinMem, FinAgent, QuantAgent, FinCON, TradingAgents): **Sharpe ratios decay 51–62% once tested past the models' training cutoff.**
- FINSABER (arXiv 2505.07078), 20 years / 100+ symbols: out-of-sample LLM alpha **+20.7% → −1.0%.**
- Large-cap news is priced into markets in **milliseconds**. A daily brief is hours late by design — anything still actionable at that cadence is context, not signal.
- Evidence is weakest outside the most liquid large-cap universes; lower
  coverage does not remove transaction costs, liquidity constraints or
  out-of-sample decay.

**Design conclusion:** the value of the monitoring brief is *attention routing*
(nothing about your holdings escapes you) and *panic prevention* — not alpha.
Its compose step hard-forbids buy/sell/hold language and ships a plain data
digest if the model deviates. The separate IBKR Daily View uses a disclosed,
deterministic portfolio screen; it does not claim that headline sentiment is an
edge and it cannot execute an order.

## Question 2: do multiple models debating produce better analysis?

**No for daily use — debate measurably hurts.**

- arXiv 2605.30802 (1,189 financial questions, 3 model families): independent aggregation with confidence-weighted voting scored **83.4%**, the best single model **82.4%**, and **deliberative multi-model consensus ~76% — below every single-model baseline.** Cause: persuasive-error propagation and sycophancy — a confidently wrong model flips a correct one.
- arXiv 2502.08788: debate rarely beats self-consistency at matched compute; it cannot exceed its strongest participant.

**Design conclusion:** one strong model composes the brief. If a second opinion is ever added, the correct shape is a different-family model as a *disagreement detector* on facts — never a debate panel.
