import json
import re

from pydantic import ValidationError

from agents.knowledge import retrieve
from agents.llm import LLMClient
from api.schemas import Manifest
from api.telemetry import span

MAX_ATTEMPTS = 3

SYSTEM = """You are a travel planner. Convert the user's request into ONE JSON object that
conforms exactly to this JSON schema. Output only JSON. Never invent prices or bookings.
Use the provided FACTS for grounding; if a field is unknown use the schema default.
SCHEMA:
{schema}"""


class PlannerError(Exception):
    pass


def extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise TypeError("JSON root is not an object")
    return obj


def plan(query: str, llm: LLMClient) -> Manifest:
    system = SYSTEM.format(schema=json.dumps(Manifest.model_json_schema()))
    facts = "\n".join(f"- {f}" for f in retrieve(query)) or "- (none)"
    user = f"FACTS:\n{facts}\n\nQUERY: {query}"
    error = ""
    with span("planner.plan", query_chars=len(query)) as sp:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = user
            if error:
                prompt = f"{user}\n\nYour previous output was invalid: {error}\nReturn corrected JSON only."
            raw = llm.complete(system, prompt)
            try:
                manifest = Manifest.model_validate(extract_json(raw))
            except (ValueError, TypeError, ValidationError) as e:
                error = str(e)[:500]
                continue
            sp.set_attribute("attempts", attempt)
            return manifest
        sp.set_attribute("attempts", MAX_ATTEMPTS)
        raise PlannerError(f"planner failed after {MAX_ATTEMPTS} attempts: {error}")
