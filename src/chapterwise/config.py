"""Settings read from the environment (and a local .env file, if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_MAX_CHUNK_TOKENS = 24_000


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def normalize_database_url(url: str | None) -> str | None:
    """Accept plain postgres:// URLs and use the psycopg 3 driver for them."""
    if not url:
        return None
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@dataclass(frozen=True)
class Settings:
    # LLM
    api_key: str | None
    base_url: str
    model: str
    max_chunk_tokens: int
    # CLI cache (the service caches in Postgres instead)
    cache_dir: Path
    # Service
    database_url: str | None = None
    books_dir: Path = Path("/books")
    secret_key: str | None = None
    admin_password: str | None = None
    public_url: str = "http://localhost:8100"
    scan_interval: int = 600  # seconds between library scans

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        env = os.environ.get
        return cls(
            api_key=env("DEEPSEEK_API_KEY") or None,
            base_url=env("CHAPTERWISE_BASE_URL") or DEFAULT_BASE_URL,
            model=env("CHAPTERWISE_MODEL") or DEFAULT_MODEL,
            max_chunk_tokens=_int("CHAPTERWISE_MAX_CHUNK_TOKENS", DEFAULT_MAX_CHUNK_TOKENS),
            cache_dir=Path(env("CHAPTERWISE_CACHE_DIR") or ".cache/chapterwise"),
            database_url=normalize_database_url(env("DATABASE_URL")),
            books_dir=Path(env("CHAPTERWISE_BOOKS_DIR") or "/books"),
            secret_key=env("CHAPTERWISE_SECRET_KEY") or None,
            admin_password=env("CHAPTERWISE_ADMIN_PASSWORD") or None,
            public_url=(env("CHAPTERWISE_PUBLIC_URL") or "http://localhost:8100").rstrip("/"),
            scan_interval=_int("CHAPTERWISE_SCAN_INTERVAL", 600),
        )

    def require(self, *names: str) -> None:
        """Fail early with a clear message when a service setting is missing."""
        env_names = {
            "database_url": "DATABASE_URL",
            "secret_key": "CHAPTERWISE_SECRET_KEY",
            "admin_password": "CHAPTERWISE_ADMIN_PASSWORD",
        }
        missing = [env_names.get(n, n) for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(f"Missing required setting(s): {', '.join(missing)}")
