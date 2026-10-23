# Pricing research implementation — 23 October 2026

Active in the local application, with manual approval retained. Run **52** has **120 pending recommendations**. No prices were published and no financial experiment was started.

Implemented:

- Joint room-price, bed-price and flex-allocation selection minimizing worst modeled regret across nine elasticity scenarios.
- Stress reports for final prices and capacity after all controls, visible on the economics page.
- Capacity/block exclusions and an audit of eligible online-learning windows.
- A recent completed-date forecast monitor that reports insufficient evidence rather than inventing results.
- Experiment fingerprints covering the new decision rule and economic-basis calculation.

Verification:

- **325 tests passed** in the full suite; focused checks also passed after the final learning/page refinements.
- Replay: **121 dates, 120 computed, zero price-floor/shared-capacity violations**. The existing 31 October constraint conflict still freezes that date.
- Compared with nominal optimization on the same inputs: **43 room prices, one bed price and 22 flex allocations changed**. These are mechanical decision changes, not measured financial gains.
- Persisted run 52: all final price/capacity diagnostics match the proposals; all remain pending.
- Desktop 1280 px and phone 390 px: stress table opens/closes, 240 product-date rows render, no page overflow or browser errors.
- Forecast monitor: only three completed one-day observations per product and none at longer horizons; all horizons remain insufficient for assessment.

The elasticity stress points are engineering assumptions, not estimates from the papers or confidence bounds. The algorithm is a one-date adaptation of minimax-regret thinking. It does not inherit a paper's sequential-learning or baseline safety guarantees. Price response still needs identifiable, time-bounded actual-price observations and a prospective financial comparison.

The [research review](../../docs/research-pricing-2026-10-23.md) links fourteen primary papers, distinguishes full-text examination from abstracts/previews, and explains which methods were implemented, adapted or deferred. It is a focused review of relevant research, not an exhaustive review of every publication.
