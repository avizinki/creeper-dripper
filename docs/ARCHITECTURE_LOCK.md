# Architecture Lock

## 1. Source of truth
- `creeper-dripper` is the production architecture source of truth.
- `hachi_swap_only_drip_sell.py` is reference-only.

## 2. Non-regression rules
- No regression to `/order -> /execute` as the primary execution model.
- No no-RPC execution assumptions.
- No trusting external execute response as full execution truth.
- No HACHI-specific hardcoding in the generic core.

## 3. Current approved execution model
- Discovery/routing via Birdeye + Jupiter.
- Pre-entry buy/sell route proof.
- Economic sanity gating.
- Transaction build -> sign -> send.
- RPC-backed execution truth.
- Reconciliation and recovery.

## 4. Future-port candidates
- Dynamic exit chunk competition.
- Liquidity exhaustion grading.
- Adaptive pacing.

## 5. Keep isolated
- Token-specific HACHI tactics remain experimental only.
