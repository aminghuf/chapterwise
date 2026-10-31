"""A content-addressed cache so the same request never hits the API twice.

Phase 1 stores JSON files on disk; the service will swap in a Postgres-backed cache with the same interface.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


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
