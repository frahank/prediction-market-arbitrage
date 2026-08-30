# Final project verdict

## Failed / not viable

The project did not find enough evidence of reliable, executable Kalshi–Polymarket
arbitrage to justify further development toward live execution.

The negative result is the useful result:

- an early bid/ask normalization error inflated apparent opportunities;
- correct book semantics and real venue fees removed the earlier attractive result;
- low-skew concurrent polling found no qualifying, non-basis episode in the initial
  reviewed cohort;
- the gaps that persisted were consistent with market basis, settlement differences,
  or long-duration collateral carry rather than free, executable profit;
- displayed-book survival was never converted into real fill or realized-P&L proof;
- later broader/streaming work did not produce enough clean, repeated evidence to
  overturn the failure verdict.

The tests covered only selected reviewed markets and therefore cannot prove that no
cross-venue arbitrage exists anywhere. They show only that this implementation and
its tested strategy did not produce enough clean, repeated, executable evidence to
justify deployment.

Accordingly, “failed” here means **the project failed to demonstrate a strategy
worth deploying**. It is not a claim that cross-venue prediction-market arbitrage
can never exist.

The repository remains useful as a research artifact: it contains public-data
discovery, concurrent polling, authenticated Kalshi market-data streaming, public
Polymarket streaming, normalization, fee and survival analysis, safety controls,
tests, and documented failure modes.
