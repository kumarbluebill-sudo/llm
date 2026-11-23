# Integrations (what each key does, and what it cannot do)

Keys live in `env.local` (git-ignored). Tests never read it (`TRAVEL_LLM_NO_ENV_FILE=1`); evals force every
live supplier off so they cannot spend quota. Opt-in live checks: `RUN_LIVE_TESTS=1 pytest -k live`.

| Provider | Used for | Limits / caveats |
|---|---|---|
| SerpApi (Google Flights, Google Hotels) | live fares and hotel rates | search only; **no fare rules** so refundability is "unverified"; plans built from it are comparison-only and cannot be booked; 250 searches/month free (30 held in reserve) |
| Viator (sandbox) | activity suggestions | read-only; production key is used only when `VIATOR_API_ENV=production` **and** `APP_ENV=production` |
| AviationStack | live flight status for the disruption poller | free plan ~100 requests/month, budget-guarded; needs real flight numbers (only live fares carry them) |
| Mapbox | airport to venue driving time | time/distance are real, the fare is a per-km **estimate**. Geocoding is validated (city centre must be near its airport, venue near the city centre) because naive lookups returned the wrong city (Mohali for Dharamshala, a far-away "Delhi") |
| FreeLLMAPI (local proxy, OpenAI-compatible) | planner LLM (`LLM_PROVIDER=freellm`) | free upstream models are flaky (empty/truncated JSON, 502s); the planner retries and the eval harness measures the result |
| Travel Desk (B2B tours API, **test** environment) | adapter only; not wired into planning | UAE inventory in AED. Has real `reserve -> confirm -> cancel` with a client `referenceNumber`, which fits our saga. Booking calls are gated (see below) |
| Anthropic, Amadeus, Razorpay | not configured | needed for: best planner model, real fares + fare rules + booking, payments |

## Travel Desk booking gate
`reserve`, `confirm` and `cancel` are refused unless `TRAVELDESK_ALLOW_LIVE_BOOKING=true` **and** the key's issuer is the
test environment **and** the caller opts in with `allow_booking=True`. Nothing in the app calls them today. A key is only ever
sent to the host matching its issuer claim (test key -> `testapi.traveldesk.me`).
