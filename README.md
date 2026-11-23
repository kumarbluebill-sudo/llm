# Travel LLM

Agentic AI travel brain: plans itineraries, books safely via a saga workflow, and recovers from disruptions.

- Full spec: [docs/PROJECT_DOCUMENT.md](docs/PROJECT_DOCUMENT.md)
- Analysis and corrections: [docs/ANALYSIS.md](docs/ANALYSIS.md)
- Release roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)

## Run locally

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Windows; use .venv/bin on Linux/macOS
.venv/Scripts/uvicorn api.main:app --port 8000
cd frontend && npm install && npm run dev                        # http://localhost:5173
```

The API uses a mock LLM and mock suppliers by default (`LLM_PROVIDER=anthropic` for real Claude).
Docker: `docker build -t travel-llm . && docker run -p 8000:8000 travel-llm`.

## Full stack (Temporal saga + Postgres)

```bash
docker compose -f infra/docker-compose.yml up -d --build   # postgres, redis, temporal, api, worker
```

Without `TEMPORAL_HOST` + `POSTGRES_URL` the API runs the same saga in-process with an in-memory store.
Suppliers and payments are mocks (`saga/mock_providers.py`); Razorpay test mode is not wired yet.

## Disruption simulation (API)

After a booking is `TICKETED`: `POST /disrupt/{booking_id}` with `{"type": "CANCELLATION", "notice_hours": 3}`
runs triage, impact analysis, held rebooking options, entitlements and notification; then
`POST /disruption/{case_id}/choose` (or `/dismiss`). In-process mode only.

## Live data (optional)

Put keys in `env.local` (git-ignored): `VIATOR_SANDBOX_KEY` gives real activity suggestions; `AVIATIONSTACK_API`
enables live flight-status polling (free plan: ~100 requests/month, the client enforces a budget).
`SERPAPI_API_KEY` gives live Google Flights/Hotels prices (search only; plans built from it are comparison-only and cannot be booked;
refundability shows as unverified). Free plan = 250 searches/month; `SERPAPI_LIVE=0` forces sample data.
`RUN_LIVE_TESTS=1 pytest tests/test_live_data.py tests/test_serpapi.py` runs the opt-in live checks.

## Evaluation and tracing

`python -m evals.run` runs the golden set and red-team checks (see docs/EVAL.md for how to read the results).
Set `OTEL_TRACES_EXPORTER=console` (or `OTEL_EXPORTER_OTLP_ENDPOINT`) to export traces.
Reconciliation: `POST /admin/reconcile` with header `x-admin-token` (enabled only when `ADMIN_TOKEN` is set).

Golden rule: the LLM proposes, deterministic code commits, the LLM never touches money.
