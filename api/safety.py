"""Input hardening: payment-card data must never reach the LLM, logs or storage."""
import re

MAX_QUERY_CHARS = 500

_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_pan(text: str) -> bool:
    return any(_luhn(re.sub(r"\D", "", m.group())) for m in _CANDIDATE.finditer(text))


def redact_pan(text: str) -> str:
    """Replace Luhn-valid 13-19 digit runs (a likely card number) before the text goes anywhere."""

    def repl(m: re.Match) -> str:
        return "[REDACTED]" if _luhn(re.sub(r"\D", "", m.group())) else m.group()

    return _CANDIDATE.sub(repl, text)
