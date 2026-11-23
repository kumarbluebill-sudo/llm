"""Tiny JSON-over-HTTPS transport. Errors never include the URL (it may carry an API key)."""
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# (method, url, headers, json_body) -> (status, parsed_json)
Transport = Callable[[str, str, dict[str, str], dict | None], tuple[int, Any]]


class HttpError(Exception):
    pass


def urllib_transport(method: str, url: str, headers: dict[str, str], body: dict | None = None) -> tuple[int, Any]:
    if not url.startswith("https://"):
        raise HttpError("refusing non-HTTPS request")  # keys must never travel in clear text
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise HttpError(f"network error: {type(e).__name__}") from None
