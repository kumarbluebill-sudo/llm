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

Golden rule: the LLM proposes, deterministic code commits, the LLM never touches money.
