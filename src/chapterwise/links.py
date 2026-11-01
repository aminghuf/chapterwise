"""Signed read links: open one section's article without logging in (used by the Telegram bot).

A link names a book and a section and is signed with CHAPTERWISE_SECRET_KEY, so it can't be altered to
reach anything else. It only grants reading; links expire after READ_LINK_MAX_AGE.
"""

from __future__ import annotations

from itsdangerous import BadSignature, URLSafeTimedSerializer

READ_LINK_MAX_AGE = 120 * 24 * 3600  # seconds
_SALT = "chapterwise.read-link"


def make_read_token(secret_key: str, document_id: int, position: int) -> str:
    return URLSafeTimedSerializer(secret_key, salt=_SALT).dumps({"d": document_id, "p": position})


def read_token_target(secret_key: str, token: str) -> tuple[int, int] | None:
    """(document_id, position) for a valid, unexpired token; None otherwise."""
    try:
        data = URLSafeTimedSerializer(secret_key, salt=_SALT).loads(token, max_age=READ_LINK_MAX_AGE)
    except BadSignature:  # includes SignatureExpired
        return None
    if not isinstance(data, dict):
        return None
    document_id, position = data.get("d"), data.get("p")
    if not isinstance(document_id, int) or not isinstance(position, int):
        return None
    return document_id, position


def read_url(public_url: str, secret_key: str, document_id: int, position: int) -> str:
    return f"{public_url}/r/{make_read_token(secret_key, document_id, position)}"
