"""Load local env files (.env, env.local, .env.local). Real environment variables win.
Returns file NAMES only; values are never logged. Tests set TRAVEL_LLM_NO_ENV_FILE=1 so they
never read real keys or hit real services."""
import os
from pathlib import Path

_FILES = (".env", "env.local", ".env.local")


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_env(root: Path | None = None, *, force: bool = False) -> list[str]:
    if os.getenv("TRAVEL_LLM_NO_ENV_FILE") and not force:
        return []
    root = root or Path(__file__).resolve().parents[1]
    loaded: list[str] = []
    for name in _FILES:
        path = root / name
        if not path.is_file():
            continue
        for k, v in _parse(path.read_text(encoding="utf-8")).items():
            if v:  # empty placeholders must not shadow real values
                os.environ.setdefault(k, v)
        loaded.append(name)
    return loaded
