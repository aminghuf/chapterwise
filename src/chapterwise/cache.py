"""A content-addressed cache so the same request never hits the API twice.

The CLI stores JSON files on disk; the service uses Postgres. Both share one interface.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from chapterwise.db import CacheEntry


class Cache(Protocol):
    def get(self, key: dict[str, Any]) -> dict[str, Any] | None: ...

    def set(self, key: dict[str, Any], value: dict[str, Any]) -> None: ...


def cache_digest(key: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class FileCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: dict[str, Any]) -> Path:
        digest = cache_digest(key)
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(key).read_text(encoding="utf-8"))["value"]
        except (OSError, ValueError, KeyError):
            return None

    def set(self, key: dict[str, Any], value: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"key": key, "value": value, "created_at": datetime.now(UTC).isoformat()}
        # Write to a temp file and rename, so an interrupted run never leaves a half-written entry.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


class PostgresCache:
    """The same cache in the service's database (table llm_cache)."""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    def get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        with self._sessions() as session:
            entry = session.get(CacheEntry, cache_digest(key))
            return entry.value if entry else None

    def set(self, key: dict[str, Any], value: dict[str, Any]) -> None:
        stmt = insert(CacheEntry).values(digest=cache_digest(key), key=key, value=value)
        stmt = stmt.on_conflict_do_update(index_elements=[CacheEntry.digest], set_={"value": value})
        with self._sessions() as session:
            session.execute(stmt)
            session.commit()

    def existing(self, digests: list[str]) -> set[str]:
        """Which of these digests are cached (one query, for list pages)."""
        if not digests:
            return set()
        with self._sessions() as session:
            return set(session.scalars(select(CacheEntry.digest).where(CacheEntry.digest.in_(digests))))
