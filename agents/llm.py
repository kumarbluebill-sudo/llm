"""LLM client abstraction. Planner depends only on `LLMClient`, so the mock and real
Claude client are interchangeable."""
import json
import os
import re
import uuid
from datetime import date, timedelta
from typing import Protocol

from agents.knowledge import CITIES


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class AnthropicLLM:
    """Real client. Not exercised by tests (needs ANTHROPIC_API_KEY); verify manually."""

    def __init__(self, model: str | None = None):
        import anthropic  # lazy: optional dependency

        self._client = anthropic.Anthropic()
        self._model = model or os.getenv("PLANNER_MODEL", "claude-sonnet-5-5")

    def complete(self, system: str, user: str) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")


class LLMError(Exception):
    """The model endpoint failed (network, quota, upstream error). Never carries credentials."""


class OpenAICompatLLM:
    """Any OpenAI-compatible /chat/completions endpoint, e.g. the local FreeLLMAPI proxy.
    Free upstream models are flaky (empty/truncated output, timeouts); the planner's validate-and-
    retry loop is what makes them usable, and the eval harness measures how well."""

    def __init__(self, base_url: str, api_key: str, model: str, transport=None, max_tokens: int = 900, timeout: float = 120):
        from mcp_servers.http import urllib_transport

        self._base, self._key, self._model = base_url.rstrip("/"), api_key, model
        self._transport, self._max_tokens, self._timeout = transport or urllib_transport, max_tokens, timeout

    @classmethod
    def from_env(cls, **kw) -> "OpenAICompatLLM":
        base, key = os.getenv("FREELLMAPI_BASE_URL"), os.getenv("FREELLMAPI_API_KEY")
        if not base or not key:
            raise LLMError("FREELLMAPI_BASE_URL / FREELLMAPI_API_KEY are not set")
        return cls(base, key, os.getenv("FREELLMAPI_MODEL", "auto"), **kw)

    def complete(self, system: str, user: str) -> str:
        from mcp_servers.http import HttpError

        body = {
            "model": self._model, "temperature": 0, "max_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }  # fmt: skip
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            try:
                status, data = self._transport("POST", f"{self._base}/chat/completions", headers, body, self._timeout)
            except TypeError:  # injected transports (tests) may not take a timeout
                status, data = self._transport("POST", f"{self._base}/chat/completions", headers, body)
        except HttpError as e:
            raise LLMError(str(e).replace(self._key, "***")) from None
        if status != 200 or not isinstance(data, dict):
            detail = ""
            if isinstance(data, dict):
                detail = str((data.get("error") or {}).get("message", ""))[:120]
            raise LLMError(f"llm http {status} {detail}".replace(self._key, "***").strip())
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("llm returned no choices") from None
        if not isinstance(text, str) or not text.strip():
            raise LLMError("llm returned empty content")
        return text


_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


def _parse_budget(q: str) -> int:
    if m := re.search(r"(\d+(?:\.\d+)?)\s*k\b", q, re.IGNORECASE):
        return int(float(m.group(1)) * 1000)
    if m := re.search(r"(?:₹|rs\.?|inr|under|budget of)\s*([\d,]{4,})", q, re.IGNORECASE):
        return int(m.group(1).replace(",", ""))
    return 30000


def _next_date(month: int, day: int) -> date:
    today = date.today()
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


def _parse_date(q: str) -> date:
    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", q):
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*", q, re.IGNORECASE)
    if m and m[2].lower() in _MONTHS:
        return _next_date(_MONTHS[m[2].lower()], int(m[1]))
    m = re.search(r"\b([a-z]{3})[a-z]*\s+(\d{1,2})\b", q, re.IGNORECASE)
    if m and m[1].lower() in _MONTHS:
        return _next_date(_MONTHS[m[1].lower()], int(m[2]))
    return date.today() + timedelta(days=30)


class MockLLM:
    """Deterministic heuristic stand-in that emits Manifest JSON. Proves the plumbing
    (prompting, extraction, validation, retry), NOT real model quality."""

    def complete(self, system: str, user: str) -> str:
        q = user.split("QUERY:", 1)[-1].strip()
        low = q.lower()
        venue = next((c for c in CITIES if c.lower() in low), None)
        teams = re.search(r"\b([A-Z]{2,4})\s+vs\.?\s+([A-Z]{2,4})\b", q)
        adults = 1
        if (m := re.search(r"(\d+)\s*(?:people|persons|adults|friends|of us)", low)) or (m := re.search(r"\bfor\s+(\d+)\b", low)):
            adults = int(m.group(1))
        elif "couple" in low:
            adults = 2
        if re.search(r"cheap|budget|backpack", low):
            tier = "budget"
        elif re.search(r"luxury|premium|5-star", low):
            tier = "premium"
        else:
            tier = "mid"
        manifest: dict = {
            "trip_id": str(uuid.uuid4()),
            "intent": "event_travel" if teams else "leisure",
            "travelers": {"adults": adults, "children": 0},
            "budget": {"tier": tier, "max_total": _parse_budget(q), "currency": "INR"},
            "constraints": {
                "refundable_only": "non-refundable ok" not in low,
                "max_layovers": 0,
                "arrival_buffer_hours": 4,
            },
            "segments": ["flight_out", "hotel", "transfer_in", "transfer_out", "flight_return"],
        }
        if venue:
            name = f"{teams[1]} vs {teams[2]}" if teams else f"Trip to {venue}"
            manifest["event"] = {"name": name, "venue": venue, "date": _parse_date(q).isoformat()}
            if teams:
                manifest["segments"].insert(3, "event")
        return "```json\n" + json.dumps(manifest) + "\n```"
