"""Settings read from the environment (and a local .env file, if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_MAX_CHUNK_TOKENS = 24_000


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    model: str
    cache_dir: Path
    max_chunk_tokens: int

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        max_chunk = os.environ.get("CHAPTERWISE_MAX_CHUNK_TOKENS", "").strip()
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            base_url=os.environ.get("CHAPTERWISE_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("CHAPTERWISE_MODEL") or DEFAULT_MODEL,
            cache_dir=Path(os.environ.get("CHAPTERWISE_CACHE_DIR") or ".cache/chapterwise"),
            max_chunk_tokens=int(max_chunk) if max_chunk else DEFAULT_MAX_CHUNK_TOKENS,
        )
