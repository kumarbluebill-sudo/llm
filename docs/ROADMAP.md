# Roadmap (versioned releases)

| Version | Weeks | Deliverable | Exit test |
|---|---|---|---|
| v0.1 Skeleton | 1 | Repo layout, FastAPI `/health` + `/plan` stub, docker compose (postgres, redis, temporal), CI | CI green; `docker compose up` works |
| v0.2 Planner | 2–3 | Planner agent -> validated Manifest (Pydantic), 20-query set, RAG (Chroma) | 100% schema-valid on 20 queries |
| v0.3 Pricer + Aggregator | 4–5 | MCP servers (flights, hotels, transport, weather) with circuit breaker/retry/cache + mock fallback, refundable filter, 3 budget tiers | End-to-end `/plan` for "RCB vs GT, Dharamshala" |
| v0.4 Frontend | 6 | React card UI with SSE; API on Fly/Render, UI on Netlify | Public demo URL |
| v0.5 Booking saga | 7–8 | Temporal workflow, idempotency table, event-sourced state machine, Razorpay test mode, HITL gates, mock suppliers | Fault-injection: all failures compensate; 0 duplicates |
| v0.6 Disruption | 9–10 | Detect -> triage -> impact graph -> options -> entitlements -> notify -> bounded executor | Simulated cancellation -> options < 90 s |
| v0.7 Eval + hardening | 11 | 300+ golden set, red-team set, tracing, reconciliation job | Section 2.3 targets measured |
| v1.0 Pilot | 12+ | Real supplier/payment agreements, DPDP + terms | Gated on partner contracts |
| v2 | later | Optional QLoRA fine-tune | Beats hosted baseline at lower cost |

Rules: idempotency wrapper on every mutating call; Pydantic + policy check before any supplier/payment call; tests first for saga, entitlements, budget; one PR per milestone, tag `vX.Y.Z`.
