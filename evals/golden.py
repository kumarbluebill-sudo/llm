"""Deterministic generator for the golden planner set (300+ cases with known ground truth).

These are SYNTHETIC cases written by us. They check that a planner extracts what a query says;
they are not real user traffic and say nothing about how real users phrase requests. The
"hard" category is deliberately beyond the mock planner so the eval can fail visibly."""
import random
from dataclasses import dataclass, field

CITY_TEAMS = {
    "Dharamshala": ["PBKS", "RCB", "GT"],
    "Mumbai": ["MI", "CSK", "RR"],
    "Bengaluru": ["RCB", "MI", "CSK"],
    "Chennai": ["CSK", "KKR", "SRH"],
    "Kolkata": ["KKR", "RR", "GT"],
    "Delhi": ["DC", "CSK", "LSG"],
    "Ahmedabad": ["GT", "MI", "DC"],
    "Hyderabad": ["SRH", "LSG", "PBKS"],
    "Jaipur": ["RR", "PBKS", "KKR"],
    "Lucknow": ["LSG", "GT", "SRH"],
}
ALIASES = {"Bengaluru": "Bangalore", "Mumbai": "Bombay", "Chennai": "Madras", "Kolkata": "Calcutta"}
BUDGETS = [12000, 15000, 18000, 20000, 25000, 28000, 30000, 35000, 40000, 45000, 50000, 60000, 75000, 90000, 120000, 150000]
MONTHS = ["April", "May"]


@dataclass
class GoldenCase:
    id: str
    query: str
    venue: str
    max_total: int
    adults: int
    category: str  # standard | hard
    tier: str | None = None
    refundable_only: bool = True
    tags: list[str] = field(default_factory=list)


def _budget_text(rng: random.Random, n: int, hard: bool) -> str:
    if hard:
        lakh = n / 100000
        words = {15000: "fifteen thousand", 25000: "twenty five thousand", 30000: "thirty thousand",
                 50000: "fifty thousand", 120000: "1.2 lakh", 150000: "1.5 lakh", 90000: "ninety thousand"}  # fmt: skip
        return words.get(n, f"{lakh:g} lakh" if n >= 100000 else f"{n // 1000}k")
    form = rng.choice(["k", "rupee", "rs", "inr", "under", "budget"])
    k = n // 1000 if n % 1000 == 0 else n / 1000
    return {
        "k": f"{k:g}k", "rupee": f"₹{n:,}", "rs": f"Rs. {n:,}", "inr": f"inr {n}",
        "under": f"under {n}", "budget": f"budget {k:g}k",
    }[form]  # fmt: skip


def _people(rng: random.Random, n: int, hard: bool) -> str:
    if hard:
        return rng.choice({1: ["just me", "alone"], 2: ["two of us", "me and my wife"], 4: ["family of 4", "four of us"]}.get(n, [f"{n} of us"]))
    forms = [f"{n} people", f"for {n}", f"{n} adults", f"{n} friends"]
    if n == 1:
        forms = ["solo", "1 adult"]
    if n == 2:
        forms += ["couple"]
    return rng.choice(forms)


def generate(n: int = 320, seed: int = 7) -> list[GoldenCase]:
    rng = random.Random(seed)
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    attempts = 0
    while len(cases) < n and attempts < n * 50:
        attempts += 1
        city = rng.choice(list(CITY_TEAMS))
        a, b = rng.sample(CITY_TEAMS[city], 2)
        budget = rng.choice(BUDGETS)
        pax = rng.choice([1, 2, 2, 3, 4, 5, 6])
        hard = rng.random() < 0.25
        tags: list[str] = []
        place = city
        if hard and city in ALIASES and rng.random() < 0.5:
            place = ALIASES[city]
            tags.append("alias")
        people = _people(rng, pax, hard and pax in (1, 2, 4) and rng.random() < 0.6)
        if people in {"just me", "alone", "two of us", "me and my wife", "family of 4", "four of us"} or " of us" in people:
            tags.append("phrase")
        btxt = _budget_text(rng, budget, hard and rng.random() < 0.5)
        if not btxt[0].isdigit() and not btxt.startswith(("₹", "Rs", "inr", "under", "budget")):
            tags.append("budget_words")
        tier = None
        prefix = ""
        if rng.random() < 0.2:
            tier, prefix = rng.choice([("budget", "Cheap trip "), ("premium", "Luxury weekend ")])
        tail = ""
        refundable = True
        if rng.random() < 0.1:
            tail, refundable = " non-refundable ok", False
        template = rng.choice(
            [f"{a} vs {b}, {place}, {people}, {btxt}", f"{prefix}{a} vs {b} at {place} for {people} {btxt}",
             f"{prefix}{a} vs {b} {place} {btxt}, {people}"]
        )  # fmt: skip
        query = template + tail
        if query in seen:
            continue
        seen.add(query)
        cases.append(
            GoldenCase(
                id=f"g{len(cases):03d}", query=query, venue=city, max_total=budget, adults=pax,
                category="hard" if tags else "standard", tier=tier, refundable_only=refundable, tags=tags,
            )
        )
    return cases
