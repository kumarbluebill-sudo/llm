# Analysis of PROJECT_DOCUMENT.md

## Keep
- Golden rule: the LLM proposes, deterministic code commits, the LLM never touches money.
- Saga with compensation, idempotency keys, authorize-then-capture, HITL thresholds, rule-based entitlements, nightly reconciliation.
- Niche-first (domestic India event travel), refundable-first filtering.

## Corrections before building
1. **Fine-tuning is not on the critical path.** Start with a hosted model (Claude Haiku/Sonnet with tool use and schema-validated JSON). Fine-tune in v2 only if evals and cost justify it.
2. **Real booking depends on distribution access, not the LLM.** Search APIs (Amadeus self-service, SerpApi) cannot ticket. Need a consolidator/NDC/aggregator agreement. Build against mock suppliers until then.
3. **Payments:** Razorpay (auth/capture) for INR domestic; Stripe is secondary.
4. **Orchestration:** skip LangGraph-then-Temporal rewrite. Plain Python agent loop for planning; Temporal for booking and disruption from the start.
5. **Unverified numbers:** TripMind/TravelPlanner results, competitor stats and market sizes need citations. Success criteria are targets, not measurements.
6. **Placeholders/stale data:** example date 2026-04-15 is in the past; `github.com/example/tripmind` is a placeholder.
7. **Eval set** of 92 is too small; grow to 300+.
8. **Compliance gaps:** DPDP Act, PCI scope, terms for AI-made bookings, DGCA refund rules, GST invoicing.
9. **Timeline:** 14 weeks solo is optimistic; use thin vertical slices with a release every ~2 weeks (see ROADMAP.md).
