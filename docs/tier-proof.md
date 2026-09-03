# Tier proof: computed, not hardcoded

**Test:** I changed Trellis Infra's payment history and regenerated, to check whether the tier is computed or hardcoded.

| | tier | broken promises | worst days late | response rate |
|---|---|---|---|---|
| **Before** | `red` | 3 | 45 | 0.30 |
| **After** (temp change) | `amber` | 0 | 0 | 0.30 |
| **Reverted** | `red` | 3 | 45 | 0.30 |

**Why amber and not green:** zero broken promises and paid-within-terms cleared two dimensions, but the response rate stayed at 0.30.
The rule treats anything under 0.60 as an amber signal on its own, so the client couldn't reach green.

**What this proves:** the tier is derived from the ledger, so it moves when the history moves.
