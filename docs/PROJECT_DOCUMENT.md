# TRAVEL LLM — COMPLETE PROJECT DOCUMENT
**Agentic AI Travel Brain: Architecture, Build Plan, and Business Case**
Version: 1.0 | Prepared for: Founder / Engineering Lead | Date: November 2026

## TABLE OF CONTENTS
1. Executive Summary
2. Project Scope & Success Criteria
3. System Architecture
4. Technology Stack
5. Phase-by-Phase Implementation Plan (14 Weeks)
6. Detailed Component Specifications
7. Data Generation & Fine-Tuning Plan
8. Booking Saga Specification
9. Disruption Management Specification
10. Evaluation Framework
11. Deployment Plan
12. Cost & Timeline
13. Risk Register
14. Business Model & Market Sizing
15. Appendices (Schemas, File Structure, API Contracts)

---

## 1. EXECUTIVE SUMMARY
This project builds a production-grade agentic travel assistant that reasons like a human travel planner, pulls live inventory from multiple suppliers, generates complete itineraries, and executes bookings safely with cancellation-protection and automatic disruption recovery.

**Core thesis:** A general-purpose LLM fails at complex travel planning (GPT-4 achieves only 0.6% full success on the TravelPlanner benchmark). A specialized, orchestrated system reaches 93.9%. That gap is the business.

**Three pillars:**

| Pillar | What It Does |
|---|---|
| Reasoning Brain | Fine-tuned small LLM (Llama 3.1 8B / Qwen2.5-7B) decomposes requests into structured task manifests |
| Live Tool Layer | MCP servers connect to flight, hotel, transport, and weather APIs with circuit breakers + idempotency |
| Safe Execution | Temporal saga workflow with hold → authorize → confirm → ticket, plus compensation on failure |

**Budget:** Under $100 for a solo builder using QLoRA and synthetic data.
**Timeline:** 14 weeks to production MVP.
**Market:** Online travel to exceed $1 trillion by 2030; AI-in-tourism growing at 26.7% CAGR.

---

## 2. PROJECT SCOPE & SUCCESS CRITERIA

### 2.1 In Scope (v1)
- Single-country domestic travel (e.g., India)
- Flight + hotel + ground transport booking
- Event-driven travel (IPL matches, festivals, concerts)
- Multi-day itinerary generation with budget tiers
- Disruption detection and rebooking

### 2.2 Out of Scope (v2+)
- International visa processing automation
- Multi-currency settlement
- Corporate travel policy engines
- Voice-first interface

### 2.3 Success Criteria (Measured)

| Metric | Target | Source |
|---|---|---|
| JSON output validity | 100% | TripMind SFT benchmark |
| Budget compliance | ≥ 98% | TripMind |
| Grounding accuracy | ≥ 89% | TripMind |
| Red-team pass rate | ≥ 53% | TripMind adversarial set |
| Booking saga success rate | ≥ 99.5% | Internal target |
| Time-to-first-option (disruption) | < 90 sec | Internal target |
| Duplicate-booking incidents | 0 | Idempotency guarantee |

---

## 3. SYSTEM ARCHITECTURE

### 3.1 High-Level Diagram
```text
[User: "RCB vs GT, Dharamshala"]
            │
            ▼
┌──────────────────────────────────────────────┐
│  ORCHESTRATOR (LangGraph / Temporal)          │
│  - Task decomposition                         │
│  - Parallel fan-out                           │
│  - Result aggregation                         │
└──────────────────────────────────────────────┘
            │
   ┌────────┼────────┬─────────┬──────────┐
   ▼        ▼        ▼         ▼          ▼
[Planner] [Flight] [Hotel] [Transport] [Weather]
  Agent    Agent    Agent    Agent      Agent
   │        │        │         │          │
   ▼        ▼        ▼         ▼          ▼
  LLM     MCP      MCP       MCP        MCP
          Srv      Srv       Srv        Srv
            │
            ▼
┌──────────────────────────────────────────────┐
│  BOOKING ENGINE (Temporal Saga)               │
│  HOLD → AUTHORIZE → CONFIRM → TICKET → SETTLE│
│  + Compensating actions on failure            │
└──────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────┐
│  DISRUPTION MONITOR (Event-Driven)            │
│  DETECT → TRIAGE → IMPACT → OPTIONS →         │
│  ENTITLE → NOTIFY → EXECUTE → RE-OPTIMIZE    │
└──────────────────────────────────────────────┘
```

### 3.2 Golden Rule
**The LLM proposes. Deterministic code commits. The LLM never touches money.**
Every booking is validated against a schema and policy engine before any supplier API is called.

### 3.3 Booking State Machine
```text
DRAFT → QUOTED → HELD → AUTHORIZED → CONFIRMED → TICKETED → SETTLED
  └──────┴────────┴─────────┴───────────┴──────────┴──→ EXPIRED/CANCELLED/REFUNDED
```

| State | Timeout | Failure Path |
|---|---|---|
| DRAFT | 24h | Discard |
| QUOTED | 15–30 min | Re-quote |
| HELD | 15 min–24h | Release hold |
| AUTHORIZED | 5–7 days | Void auth |
| CONFIRMED | — | Compensate |
| TICKETED | — | Refund |
| SETTLED | — | Dispute |

---

## 4. TECHNOLOGY STACK

| Layer | Choice | Rationale |
|---|---|---|
| Orchestration | LangGraph (dev) → Temporal (prod) | LangGraph for prototype; Temporal for durable saga |
| Base LLM (fine-tuned) | Llama 3.1 8B or Qwen2.5-7B | Proven in TripMind; fits on single GPU |
| Teacher LLM (data gen) | GPT-4o-mini | $4 per 5K synthetic pairs |
| Fine-tuning | QLoRA (r=8), Unsloth | 2–3 hours on RTX 3090 |
| Serving | Ollama (GGUF Q4_K_M) / vLLM | 4.6 GB model, runs on ≥4 GB VRAM |
| API | FastAPI + SSE streaming | Async, Pydantic validation |
| Tool Protocol | MCP (Model Context Protocol) | Standardized tool discovery |
| Vector Store | ChromaDB + BGE-small | Static knowledge (visa, guides) |
| State DB | PostgreSQL | Booking + saga state, append-only events |
| Cache | Redis (TTL) | Holds, quotes |
| Payments | Stripe (manual capture) / Razorpay | PCI-safe, authorize-then-capture |
| Event Bus | Kafka / SQS | Webhooks, async compensation |
| Observability | OpenTelemetry + Arize | Trace every decision and tool call |
| Frontend | React + SSE streaming | Card-based itinerary UI |

---

## 5. PHASE-BY-PHASE IMPLEMENTATION PLAN (14 WEEKS)

### Phase 1: RAG + MCP Baseline (Weeks 1–2)
**Goal:** Working agent with static knowledge (RAG) + one live tool (MCP).

Deliverables:
- ChromaDB populated with city guides, visa rules, IPL schedule
- One MCP server (py-mcp-travelplanner) exposing flight/hotel/weather tools
- LangGraph linear flow: weather → cache → live search → flights → hotels → itinerary

Validation: 20 test queries; classify failures as missing fact (→RAG) vs wrong format (→fine-tune) vs hallucination (→grounding eval).

> Tamil: இந்தக் கட்டத்தில் fine-tuning வேண்டாம். RAG மட்டுமே பெரும்பாலான தகவல் பிழைகளைத் தீர்க்கும் என்பதைச் சோதியுங்கள்.

### Phase 2: Multi-Agent Expansion (Weeks 3–4)
**Goal:** Parallel fan-out to specialized agents.

Follow TripAI's 4-stage pipeline:

| Stage | Agent | Tools | Output |
|---|---|---|---|
| 1 | Planner | None | JSON Manifest (dates, needs, budget) |
| 2 | Pricer | Playwright MCP | Live prices from 6 searches |
| 3 | Budget | Calculator MCP | 3-tier budget (budget/mid/premium) |
| 4 | Aggregator | None | Final JSON dossier |

Key decision: Use small models per agent (e.g., Claude Haiku or Qwen2.5-3B). One monolithic model causes latency and error cascade.

> Tamil: ஒவ்வொரு முகவருக்கும் தனி சிறிய LLM பயன்படுத்துங்கள். ஒரே பெரிய மாடல் எல்லாவற்றையும் செய்வது தாமதத்தையும் பிழைகளையும் ஏற்படுத்தும்.

### Phase 3: MCP Server Design (Weeks 5–6)
**Goal:** Production-grade supplier integrations with resilience.

Required MCP servers:

| Server | Provider | Tools |
|---|---|---|
| Flights | Amadeus / SerpApi | search_flights, get_fare_rules, hold_flight |
| Hotels | Booking.com / Ktemia | search_hotels, check_cancellation_policy, hold_room |
| Transport | Google Maps / OpenRouteService | get_route, estimate_travel_time |
| Weather | OpenWeatherMap | forecast, alerts |

Mandatory per server:
- Circuit breaker (stop hammering dead APIs)
- Exponential backoff retry
- Fallback provider
- Idempotency key on every mutating call

```python
def book_flight(booking_id, offer_id, payment_token):
    key = f"{booking_id}:flight:{offer_id}"
    existing = db.get_idempotent_result(key)
    if existing:
        return existing
    db.record_attempt(key, status="IN_PROGRESS")
    resp = supplier.book(offer_id, payment_token, idempotency_key=key)
    db.record_result(key, resp)
    return resp
```

> Tamil: Idempotency key இல்லாவிட்டால், நெட்வொர்க் டைம்அவுட் இரட்டை டிக்கெட் மற்றும் சார்ஜ்பேக் உருவாக்கும்.

### Phase 4: Fine-Tuning (Weeks 7–8) — Only if RAG insufficient
**Goal:** Fill behavioral gaps (structured JSON, reasoning patterns).

TripMind empirical results (critical):

| Approach | JSON Valid | Grounding | Verdict |
|---|---|---|---|
| SFT (5,000 clean pairs) | 100% | 89.5% | Winner |
| Distillation (500 traces) | 92.4% | 44.2% | Underperformed |
| Two-stage curriculum | 10.9% | — | Catastrophic |

Lesson: Use plain SFT only. Avoid curriculum learning and distillation in v1.

Training recipe:
- Base: Llama 3.1 8B
- Method: QLoRA (4-bit), r=8, Unsloth
- Hardware: RTX 3090 (24 GB)
- Time: 2–3 hours per 1K samples
- Cost: ~$4–$5 on RunPod/Vast.ai
- Export: Merge LoRA → GGUF Q4_K_M (~4.6 GB) → register with Ollama

> Tamil: 5,000 சுத்தமான ஜோடிகளை GPT-4o-mini மூலம் $4க்கு உருவாக்கலாம். பாடத்திட்ட கற்றலைத் தவிர்க்கவும்—அது JSON வெளியீட்டை உடைக்கும்.

### Phase 5: Booking Saga (Weeks 9–10)
**Goal:** Safe multi-supplier booking with compensation.

Temporal workflow:
```python
@workflow.defn
class TravelBookingWorkflow:
    @workflow.run
    async def run(self, request):
        saga = Saga()
        try:
            flight = await workflow.execute_activity(hold_flight, request.flight)
            saga.add_compensation(cancel_flight, flight)

            hotel = await workflow.execute_activity(hold_hotel, request.hotel)
            saga.add_compensation(cancel_hotel, hotel)

            payment = await workflow.execute_activity(authorize_payment, request.payment)
            saga.add_compensation(void_payment, payment)

            await workflow.execute_activity(ticket_flight, flight)
            await workflow.execute_activity(confirm_hotel, hotel)
            await workflow.execute_activity(capture_payment, payment)
        except Exception:
            await saga.compensate()
```

Rules:
- Order steps by reversibility cost — hold everything first, commit in ascending order of cancellation penalty
- Persist saga state before every external call
- Never auto-compensate forward (don't book expensive replacement without user approval)
- Human-in-the-loop gate when:
  - Total value > $2,000
  - Supplier response deviates > 2% from quote
  - Non-refundable penalty not yet acknowledged
  - Conflicting cancellation terms across suppliers

> Tamil: "கேன்சல் ஆகக்கூடாது" என்பதற்கு, Saga-வில் ரத்து செய்ய முடியாத டிக்கெட்டுகளைத் தவிர்க்கவும். Pricer முகவர் get_fare_rules மூலம் ரத்துக் கொள்கையைச் சரிபார்த்து, refundable விருப்பங்களை மட்டுமே முன்னிலைப்படுத்த வேண்டும்.

### Phase 6: Disruption Management (Weeks 11–12)
**Goal:** Act before the traveler asks.

7-stage pipeline:
1. **DETECT:** Airline NDC webhook + status API polling + email parsing (LLM)
2. **TRIAGE:** Deterministic rules (delay <60 min = Low; cancellation <24h = Critical)
3. **IMPACT:** Graph traversal — connecting flight, hotel check-in, transfer, activity, visa window
4. **OPTIONS:** 3–5 ranked options, each with live inventory hold (10 min)
5. **ENTITLE:** Rule engine for DGCA / EU261 / UK261 / DOT 24-hr
6. **NOTIFY:** Push → SMS → WhatsApp → email (consent-ordered)
7. **EXECUTE:** Bounded autonomy

Autonomy budget:

| Condition | Autonomy |
|---|---|
| Cost delta ≤ $0, same carrier, pre-approved | Auto-execute |
| Cost delta ≤ $150, within policy | Auto-execute + notify |
| Cost delta > threshold, downgrade, date change | Require approval |
| Refund/compensation claim | Always approval |
| Cross-carrier rebooking | Always approval |

Observability metrics: time-to-detection, time-to-first-option, auto-resolution rate, option acceptance rate, cascade miss rate (most expensive failure).

> Tamil: விமான ரத்து என்பது ஒரு பிரச்சினை அல்ல — இது ஒரு வரைபடப் பயணம். இணைப்பு விமானம், ஹோட்டல், டாக்ஸி, சுற்றுலா — அனைத்தையும் தாக்க மதிப்பீட்டில் சேர்க்க வேண்டும்.

### Phase 7: Evaluation (Week 13)
Golden set: 92 test cases × 10 metrics + 45 red-team prompts (TripMind template).

| Metric | Target |
|---|---|
| JSON validity | 100% |
| Budget compliance | ≥ 98% |
| Grounding accuracy | ≥ 89% |
| ROUGE-L | ≥ 43% |
| Red-team pass | ≥ 53% |

External benchmarks: TravelPlanner (GPT-4 baseline = 0.6%), ChinaTravel.

Warning: 92 cases is insufficient for large-scale confidence. Expand to 300+ before production decisions.

### Phase 8: Deployment (Week 14)

| Component | Choice |
|---|---|
| Serving | Ollama (GGUF Q4_K_M) or vLLM |
| API | FastAPI + SSE streaming |
| Frontend | React card-based UI |
| Deployment | Docker + AWS EC2 / RunPod |
| Monitoring | OpenTelemetry + Arize |

---

## 6. DETAILED COMPONENT SPECIFICATIONS

### 6.1 Planner Agent — Output Schema
```json
{
  "trip_id": "uuid",
  "intent": "event_travel",
  "event": {
    "name": "RCB vs GT",
    "venue": "Dharamshala",
    "date": "2026-04-15",
    "time": "19:30 IST"
  },
  "travelers": { "adults": 2, "children": 0 },
  "budget": { "tier": "mid", "max_total": 25000, "currency": "INR" },
  "constraints": {
    "refundable_only": true,
    "max_layovers": 0,
    "arrival_buffer_hours": 4
  },
  "segments": ["flight_out", "hotel", "transfer_in", "event", "transfer_out", "flight_return"]
}
```

### 6.2 Pricer Agent — Tool Call Sequence
1. `search_flights(origin, dest, date, refundable=true)`
2. `get_fare_rules(offer_id)` → filter non-refundable
3. `search_hotels(venue, checkin, checkout, refundable=true)`
4. `get_route(airport, venue)` → transfer time
5. `forecast(venue, date)` → weather advisory

### 6.3 Aggregator — Final Itinerary Format
```json
{
  "itinerary_id": "uuid",
  "summary": "2-night Dharamshala IPL trip",
  "total_cost": 23400,
  "currency": "INR",
  "segments": [
    { "type": "flight", "details": {}, "refundable": true, "hold_expires": "..." },
    { "type": "hotel", "details": {}, "refundable": true, "hold_expires": "..." },
    { "type": "transfer", "details": {} },
    { "type": "event", "details": {} }
  ],
  "booking_actions": [
    { "label": "Book all", "action": "commit_saga" },
    { "label": "Book flights only", "action": "partial_commit" }
  ]
}
```

---

## 7. DATA GENERATION & FINE-TUNING PLAN

### 7.1 Synthetic Data Generation

| Step | Detail |
|---|---|
| Seed set | 50K personas × travel intents |
| Teacher | GPT-4o-mini |
| Output | 5,000 validated (baseline + optimized itinerary) pairs |
| Cost | $4 |
| Filter | Discard looping tool calls, empty responses, <50 API calls |

### 7.2 Training Configuration
```yaml
base_model: meta-llama/Llama-3.1-8B-Instruct
method: qlora
quantization: 4bit
lora_r: 8
lora_alpha: 16
seq_len: 512
fp16: true
epochs: 3
learning_rate: 2e-4
batch_size: 4
gradient_accumulation: 4
hardware: RTX 3090 (24GB)
time_per_1k_samples: 2-3h
cost: $4-$5
```

### 7.3 Failure Modes to Avoid

| Risk | Evidence | Mitigation |
|---|---|---|
| Curriculum breaks JSON | 10.9% validity vs 100% | Avoid curriculum in v1 |
| Distillation underperforms | 44.2% vs 89.5% grounding | Use SFT only |
| CUDA/bitsandbytes incompat | QLoRA failed on Kaggle T4 | FP16 LoRA fallback |
| Hallucinated facts from weights | MindSpore case | Route facts through RAG |

---

## 8. BOOKING SAGA SPECIFICATION

### 8.1 State Transitions

| From | To | Trigger | Compensation |
|---|---|---|---|
| DRAFT | QUOTED | Pricer returns live prices | — |
| QUOTED | HELD | Supplier hold succeeds | Release hold |
| HELD | AUTHORIZED | Payment authorize | Void auth |
| AUTHORIZED | CONFIRMED | All suppliers confirm | Cancel each |
| CONFIRMED | TICKETED | Airline issues ticket | Refund flow |
| TICKETED | SETTLED | Nightly reconciliation | Dispute |

### 8.2 Payment Rules

| Rule | Implementation |
|---|---|
| LLM never sees card | Client-side Stripe Elements |
| Authorize before capture | `capture_method="manual"` |
| Capture only after all confirmed | Saga step 6 |
| Auto-void on failure | Saga compensation |
| Store no PAN | Token + last4 + brand only |

### 8.3 Reconciliation (Nightly)
Match three sources:
1. Internal booking records
2. Supplier confirmation feeds (GDS PNR, hotel API, airline NDC)
3. Payment processor settlements

Mismatches → tickets: ghost booking (internal only), orphan booking (supplier only). Both are silent revenue leaks.

---

## 9. DISRUPTION MANAGEMENT SPECIFICATION

### 9.1 Trigger Sources

| Source | Latency |
|---|---|
| Airline NDC/GDS queue | Seconds |
| Flight status polling | 1–5 min |
| Supplier webhook | Seconds |
| Email/PDF parsing (LLM) | Minutes |
| User-initiated | Instant |
| Weather/strike APIs | Hours |

### 9.2 Severity Classification

| Signal | Severity | Action |
|---|---|---|
| Delay < 60 min | Low | Notify only |
| Delay 60–180 min | Medium | Offer alternatives |
| Missed connection | High | Auto-generate rebooking options |
| Cancellation < 24h | Critical | HITL + auto-hold alternatives |
| Denied boarding | Critical | Entitlement + compensation |

### 9.3 Entitlement Engine (Rule-based, not LLM)
- DGCA (India): ₹5,000–₹10,000 for cancellations
- EU261: €250–€600 depending on distance
- UK261, Canada APPR, US DOT 24-hour rule
- Fare rules: rebooking vs refund eligibility
- Loyalty status: priority rebooking, fee waivers

The LLM drafts the claim; the rule engine decides eligibility.

---

## 10. EVALUATION FRAMEWORK

### 10.1 Metrics

| Category | Metric | Weight |
|---|---|---|
| Structural | JSON validity | 20% |
| Structural | Schema compliance | 15% |
| Semantic | Budget adherence | 20% |
| Semantic | Grounding accuracy | 20% |
| Reasoning | Intent alignment | 10% |
| Safety | Red-team pass | 15% |

### 10.2 Test Harness
```python
def evaluate(model, golden_set, red_team):
    results = {"structural": [], "semantic": [], "safety": []}
    for case in golden_set:
        output = model.generate(case.prompt)
        results["structural"].append(validate_json(output))
        results["semantic"].append(check_grounding(output, case.facts))
    for prompt in red_team:
        output = model.generate(prompt)
        results["safety"].append(red_team_pass(output))
    return aggregate(results)
```

---

## 11. DEPLOYMENT PLAN

### 11.1 File Structure
```text
travel-llm/
├── agents/
│   ├── planner.py
│   ├── pricer.py
│   ├── budget.py
│   └── aggregator.py
├── mcp_servers/
│   ├── flights/
│   ├── hotels/
│   ├── transport/
│   └── weather/
├── saga/
│   ├── workflow.py
│   ├── activities.py
│   └── compensations.py
├── disruption/
│   ├── detector.py
│   ├── triage.py
│   ├── entitlements.py
│   └── executor.py
├── models/
│   ├── train.py
│   ├── data_gen.py
│   └── eval.py
├── api/
│   ├── main.py
│   └── schemas.py
├── frontend/
└── infra/
    ├── docker-compose.yml
    └── terraform/
```

### 11.2 API Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | /plan | Generate itinerary |
| POST | /book | Start booking saga |
| GET | /booking/{id} | Saga status |
| POST | /disrupt/{id} | Inject disruption (test) |
| GET | /health | Liveness |

---

## 12. COST & TIMELINE

| Phase | Duration | Cost (Solo) | Cost (Outsourced) |
|---|---|---|---|
| RAG + MCP | 2 wk | $0–$50 | $5,000 |
| Multi-agent | 2 wk | $0 | $3,000 |
| MCP servers | 2 wk | $0 | $3,000 |
| Fine-tuning | 1 wk | $4–$5 | $2,000 |
| Saga | 2 wk | $0 | $3,000 |
| Disruption | 2 wk | $0 | $3,000 |
| Evaluation | 1 wk | $0 | $2,000 |
| Deployment | 2 wk | $0–$20/mo | $3,000 |
| **Total** | 14 wk | < $100 | ~$24,000 |

---

## 13. RISK REGISTER

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | LLM touches payment | Low | Critical | Payment token only; manual capture |
| 2 | Duplicate booking | Medium | Critical | Idempotency keys mandatory |
| 3 | Curriculum breaks JSON | High (proven) | High | Avoid curriculum; SFT only |
| 4 | Distillation underperforms | High (proven) | Medium | SFT only |
| 5 | Small eval set overconfidence | High | Medium | Expand to 300+ cases |
| 6 | Airline cancels AI booking | Medium | High | Warn user; prefer refundable |
| 7 | Cascade miss (hotel/transfer) | Medium | High | Graph-based impact analysis |
| 8 | Supplier API outage | High | Medium | Circuit breaker + fallback |

---

## 14. BUSINESS MODEL & MARKET SIZING

### 14.1 Market

| Segment | 2025 | 2030 | CAGR |
|---|---|---|---|
| Global tourism | $10.22T | $13.71T | 6.1% |
| Online travel | $702B | $1T+ | ~7.4% |
| AI in tourism | $3.37B | $13.86B | 26.7% |

### 14.2 Revenue Models

| Model | Description | Margin |
|---|---|---|
| Commission | 12–15% per booking | High |
| B2B SaaS | License to airlines/hotels | Medium-High |
| Subscription | Premium planning | Medium |
| Pay-per-interaction | Disruption rebooking | Low-Medium |

### 14.3 Proof Points from Competitors
- tiket.com: 10K → 75K monthly AI interactions (650% growth) without proportional headcount
- Yatra: 200+ daily cancellations, resolution time 30–40 min → < 2 min, cost < ₹1,000/month, saves ₹30 lakh/year
- Booking Holdings: $186.1B gross bookings at 14.5% take rate
- Trip.com: Saves customer service team 10,000+ working hours daily

### 14.4 Scale-Up Path
1. Niche (domestic event travel) → prove ROI in 90 days
2. Expand geographically
3. Adjacent verticals (corporate, experiences)
4. Capture 0.1% of $1T online travel = $1B in bookings

---

## 15. APPENDICES

### Appendix A: Environment Variables
```bash
OPENAI_API_KEY=...
AMADEUS_CLIENT_ID=...
AMADEUS_CLIENT_SECRET=...
STRIPE_SECRET_KEY=...
TEMPORAL_HOST=localhost:7233
POSTGRES_URL=postgresql://...
REDIS_URL=redis://...
CHROMA_PATH=./data/chroma
```

### Appendix B: Quick Start
```bash
git clone https://github.com/example/tripmind
cd tripmind
docker-compose up -d postgres redis temporal
python scripts/seed_rag.py
python scripts/generate_data.py --pairs 5000
python scripts/train_qlora.py --config configs/sft.yaml
python scripts/merge_and_quantize.py
ollama create travel-llm -f Modelfile
uvicorn api.main:app --reload
```

### Appendix C: Reference Repositories
- TripMind — full pipeline (MCP, QLoRA, eval, FastAPI)
- TripAI — 4-stage agent pipeline
- helloagents-trip-planner — LLaMA-Factory post-training tutorial
- py-mcp-travelplanner — unified MCP server

### Appendix D: Glossary

| Term | Meaning |
|---|---|
| MCP | Model Context Protocol — standard tool interface |
| Saga | Distributed transaction pattern with compensations |
| QLoRA | Quantized Low-Rank Adaptation |
| HITL | Human-in-the-loop |
| SFT | Supervised Fine-Tuning |
| GGUF | Quantized model format for Ollama |
