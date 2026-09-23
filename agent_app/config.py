"""Private, reloadable .env settings shared by the HTTP and CLI entrypoints."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AISettings:
    api_key: str = field(default="", repr=False)
    model: str = "gpt-4.1-mini"
    timeout_seconds: float = 45.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_key.lower() not in {"your-key", "...", "sk-...", "your-api-key"})


def get_ai_settings(root: str | Path | None = None) -> AISettings:
    # Read on demand so editing .env takes effect on the next click. Do not copy
    # values into os.environ: inherited environment variables retain precedence.
    values = dotenv_values(Path(root or PROJECT_ROOT) / ".env", interpolate=False, encoding="utf-8-sig")

    def value(name: str, default: str = "") -> str:
        return str(os.environ.get(name, values.get(name) or default)).strip()

    try:
        timeout = max(5.0, min(120.0, float(value("OPENAI_TIMEOUT_SECONDS", "45"))))
    except ValueError:
        timeout = 45.0
    return AISettings(value("OPENAI_API_KEY"), value("OPENAI_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini", timeout)
