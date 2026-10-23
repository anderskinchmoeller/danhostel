# Pricing model fixes — 23 October 2026

Adaptive learning now smooths toward the unadjusted observed/baseline ratio. Cancellations offset sales across the learning window. Stable deficits no longer compound indefinitely.

The automatic discount floor constrains market adjustments, smoothing, exploration and the daily brake. Conflicting bounds freeze the date; manual locks remain explicit overrides.

Prices are compared across feasible shared-inventory allocations. Fixed-room demand is valued separately from incremental flex-room sales. After final price controls, allocation and bid-price diagnostics are recalculated at the actual proposed prices.

The bed model now trains on 8 March–31 December 2025. Room-model numerical estimates are unchanged. Missing seasonal months borrow older seasonal factors only. Bed demand adjustment was rebased from 0.60 to 1.00, with the previous state and model preserved in this folder and an entry in the adjustment history.

VAT-aware model contribution and group-price conversion are active. Based on the user’s 23 October reply: both variable costs exclude recoverable VAT, and commission is interpreted as 16% of revenue excluding VAT. Guest prices include 25% VAT. At DKK 645, the room contribution is DKK 321.88; at DKK 270, the bed contribution is DKK 141.60. These remain model estimates, not measured accounting results.

## Verification

- Full test suite: 314 passed.
- Read-only replay: 120 of 121 dates recalculated, no discount-floor violations or diagnostic inconsistencies.
- Changed proposals versus run 49: 0 room prices, 9 bed prices, 54 flex allocations.
- 31 October freezes because the bed discount floor and current daily price-change limit cannot both be met. This requires review of the actual rate/anchor or an explicit manual lock.
- Fresh production run 50 now contains 120 pending proposals. 31 October is frozen with the reason recorded in the run. No prices were approved, exported or published. Existing forecast checkpoints were preserved (zero new snapshots).

## Bed-model development comparison

Training excludes all stay dates on or after 1 October 2025. Test: 92 stay dates in October–December 2025. Adaptive levels held at 1.0. The newer candidate uses the production recipe, including older factors for missing seasonal months.

| Model | Days before stay | MAE in beds | Coverage of 80% interval |
|---|---:|---:|---:|
| all_history | 1 | 2.44 | 98.9% |
| all_history | 7 | 6.20 | 88.0% |
| all_history | 14 | 7.99 | 85.9% |
| all_history | 30 | 9.20 | 88.0% |
| recent_beds | 1 | 1.16 | 73.9% |
| recent_beds | 7 | 2.47 | 81.5% |
| recent_beds | 14 | 2.67 | 83.7% |
| recent_beds | 30 | 2.69 | 90.2% |

These development results informed model selection. They are not an independent acceptance test, evidence of current 2026 accuracy, or proof of a pricing/revenue effect. The original frozen 2024→2025 validation remains unchanged. The available historical training data still ends in December 2025.

Room and bed elasticities remain unmeasured at 1.6 and 2.0. The discount protection remains enabled. Prospective validation and the planned experiment are still required.

## Supporting files

- `verification.json`: the complete replay and proposed prices, without applying them.
- `bed-regime-validation.json`: development-test metrics and caveats.
- `before-demand_model.json`: original model.
- `before-demand_level.json`: original adaptive state.

The app was restarted and its health endpoint verified with the new model and confirmed VAT basis. The full test suite passed: 314 tests. `new-run.json` records the fresh pending run.
