"""The web UI: a single-user, server-rendered app (no JavaScript)."""
# No `from __future__ import annotations` here: FastAPI must see the real types of the local DB/User aliases.

import hmac
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from chapterwise import delivery, jobs
from chapterwise.cache import PostgresCache, cache_digest
from chapterwise.config import Settings
from chapterwise.db import (
    ACTIVE_JOB_STATUSES,
    DocumentRow,
    Job,
    SectionRow,
    make_engine,
    make_session_factory,
)
from chapterwise.summarize import cache_key

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
SESSION_MAX_AGE = 30 * 24 * 3600

# Raw HTML in model output is escaped, never rendered.
_markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "script-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


class LoginRequired(Exception):
    pass


class LoginThrottle:
    """Too many wrong passwords from one address locks it out for a while."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        failures = self._failures[ip]
        while failures and failures[0] < time.monotonic() - LOGIN_WINDOW_SECONDS:
            failures.popleft()
        return len(failures) >= LOGIN_MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        self._failures[ip].append(time.monotonic())

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)


def create_app(settings: Settings, sessions: sessionmaker | None = None) -> FastAPI:
    settings.require("database_url", "secret_key", "admin_password")
    if sessions is None:
        sessions = make_session_factory(make_engine(settings.database_url))
    cache = PostgresCache(sessions)
    throttle = LoginThrottle()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["num"] = lambda n: f"{n or 0:,}"
    templates.env.filters["markdown"] = _markdown.render

    app = FastAPI(title="chapterwise", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="chapterwise_session",
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    @app.middleware("http")
    async def protect(request: Request, call_next):
        # Reject cross-site form posts (on top of SameSite cookies).
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and urlparse(source).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-site request blocked", status_code=403)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, _exc: LoginRequired):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    def db():
        with sessions() as session:
            yield session

    def user(request: Request) -> str:
        if request.session.get("user") != "admin":
            raise LoginRequired
        return "admin"

    DB = Annotated[Session, Depends(db)]
    User = Annotated[str, Depends(user)]

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {"api_key_set": bool(settings.api_key), "model": settings.model, **context},
        )

    def summary_digest(doc: DocumentRow, section: SectionRow) -> str:
        return cache_digest(
            cache_key("section", section.text_sha256, doc.title, section.title, settings.model)
        )

    def queue_counts(session: Session) -> dict[str, int]:
        rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        return {status: count for status, count in rows}

    def active_summaries(session: Session, document_id: int) -> dict[int, str]:
        rows = session.execute(
            select(Job.payload, Job.status).where(
                Job.kind == "summarize",
                Job.status.in_(ACTIVE_JOB_STATUSES),
                Job.dedupe_key.like(f"summarize:{document_id}:%"),
            )
        ).all()
        return {payload["position"]: status for payload, status in rows}

    # --- health -------------------------------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz(session: DB) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ok"}

    # --- auth ---------------------------------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return render(request, "login.html", next=next, error=None)

    @app.post("/login")
    def login(request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
        ip = request.client.host if request.client else "unknown"
        if throttle.blocked(ip):
            return render(request, "login.html", next=next, error="Too many attempts. Try again later.")
        if not hmac.compare_digest(password.encode(), settings.admin_password.encode()):
            throttle.record_failure(ip)
            return render(request, "login.html", next=next, error="Wrong password.")
        throttle.reset(ip)
        request.session.clear()
        request.session["user"] = "admin"
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(safe_next, status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- library ------------------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def library(request: Request, session: DB, _user: User, q: str = ""):
        query = select(DocumentRow).order_by(func.lower(DocumentRow.title))
        if q.strip():
            pattern = f"%{q.strip()}%"
            query = query.where(DocumentRow.title.ilike(pattern) | DocumentRow.author.ilike(pattern))
        docs = session.scalars(query).all()

        sections = session.execute(
            select(SectionRow.document_id, SectionRow.title, SectionRow.text_sha256).where(
                SectionRow.document_id.in_([d.id for d in docs])
            )
        ).all()
        by_id = {d.id: d for d in docs}
        digests: dict[int, list[str]] = defaultdict(list)
        for document_id, title, sha in sections:
            doc_title = by_id[document_id].title
            digests[document_id].append(
                cache_digest(cache_key("section", sha, doc_title, title, settings.model))
            )
        done = cache.existing([d for ds in digests.values() for d in ds])
        stats = {
            doc_id: {"sections": len(ds), "summarized": sum(1 for d in ds if d in done)}
            for doc_id, ds in digests.items()
        }
        counts = queue_counts(session)
        return render(
            request,
            "library.html",
            docs=docs,
            stats=stats,
            q=q,
            queue=counts,
            refresh=bool(counts.get("queued") or counts.get("running")),
        )

    @app.post("/rescan")
    def rescan(session: DB, _user: User):
        jobs.enqueue(session, "scan", dedupe_key="scan")
        session.commit()
        return RedirectResponse("/", status_code=303)

    # --- books --------------------------------------------------------------------------------------------

    def get_doc(session: Session, document_id: int) -> DocumentRow:
        doc = session.get(DocumentRow, document_id)
        if doc is None:
            raise HTTPException(404, "Book not found")
        return doc

    @app.get("/b/{document_id}", response_class=HTMLResponse)
    def book(request: Request, document_id: int, session: DB, _user: User):
        doc = get_doc(session, document_id)
        sections = doc.sections
        done = cache.existing([summary_digest(doc, s) for s in sections])
        active = active_summaries(session, doc.id)
        rows = [
            {
                "section": s,
                "state": "done" if summary_digest(doc, s) in done else active.get(s.position, "none"),
            }
            for s in sections
        ]
        missing = [r["section"] for r in rows if r["state"] == "none"]
        top_level = min((s.level for s in sections), default=1)
        return render(
            request,
            "book.html",
            doc=doc,
            rows=rows,
            top_level=top_level,
            missing_count=len(missing),
            missing_tokens=sum(s.tokens for s in missing),
            refresh=doc.status == "pending" or bool(active),
            telegram=bool(settings.telegram_token),
            subscription=delivery.open_subscription(session, doc.id),
            delivery_time=settings.delivery_time,
        )

    # --- daily reading on Telegram ------------------------------------------------------------------------

    @app.post("/b/{document_id}/subscribe")
    def subscribe(document_id: int, session: DB, _user: User, start: Annotated[int, Form()] = 0):
        try:
            delivery.subscribe(session, document_id, start)
        except delivery.SubscriptionError as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse(f"/b/{document_id}", status_code=303)

    @app.post("/subscriptions/{subscription_id}/{action}")
    def change_subscription(subscription_id: int, action: str, session: DB, _user: User):
        status = {"pause": "paused", "resume": "active", "stop": "stopped"}.get(action)
        if status is None:
            raise HTTPException(404)
        try:
            sub = delivery.set_status(session, subscription_id, status)
        except delivery.SubscriptionError as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse(f"/b/{sub.document_id}", status_code=303)

    @app.post("/b/{document_id}/summarize")
    def summarize(document_id: int, session: DB, _user: User, sections: Annotated[str, Form()]):
        if not settings.api_key:
            raise HTTPException(400, "DEEPSEEK_API_KEY is not set")
        doc = get_doc(session, document_id)
        if sections == "all":
            done = cache.existing([summary_digest(doc, s) for s in doc.sections])
            positions = [s.position for s in doc.sections if summary_digest(doc, s) not in done]
        else:
            try:
                positions = [int(sections)]
            except ValueError:
                raise HTTPException(400, "Invalid section") from None
            if positions[0] not in {s.position for s in doc.sections}:
                raise HTTPException(404, "Section not found")
        for position in positions:
            jobs.enqueue(
                session,
                "summarize",
                {"document_id": doc.id, "position": position},
                dedupe_key=f"summarize:{doc.id}:{position}",
            )
        session.commit()
        anchor = f"#s{positions[0]}" if len(positions) == 1 else ""
        return RedirectResponse(f"/b/{doc.id}{anchor}", status_code=303)

    @app.get("/b/{document_id}/s/{position}", response_class=HTMLResponse)
    def section_page(request: Request, document_id: int, position: int, session: DB, _user: User):
        doc = get_doc(session, document_id)
        by_position = {s.position: s for s in doc.sections}
        section = by_position.get(position)
        if section is None:
            raise HTTPException(404, "Section not found")
        entry = cache.get(cache_key("section", section.text_sha256, doc.title, section.title, settings.model))
        state = "done" if entry else active_summaries(session, doc.id).get(position, "none")
        positions = sorted(by_position)
        index = positions.index(position)
        return render(
            request,
            "section.html",
            doc=doc,
            section=section,
            summary=entry["text"] if entry else None,
            state=state,
            prev=by_position[positions[index - 1]] if index > 0 else None,
            next=by_position[positions[index + 1]] if index + 1 < len(positions) else None,
            refresh=state in ACTIVE_JOB_STATUSES,
        )

    # --- jobs ---------------------------------------------------------------------------------------------

    @app.get("/jobs", response_class=HTMLResponse)
    def job_list(request: Request, session: DB, _user: User):
        recent = session.scalars(select(Job).order_by(Job.id.desc()).limit(100)).all()
        titles = dict(
            session.execute(
                select(DocumentRow.id, DocumentRow.title).where(
                    DocumentRow.id.in_({j.payload.get("document_id") for j in recent} - {None})
                )
            ).all()
        )
        counts = queue_counts(session)
        return render(
            request,
            "jobs.html",
            jobs=recent,
            titles=titles,
            queue=counts,
            refresh=bool(counts.get("queued") or counts.get("running")),
        )

    @app.post("/jobs/{job_id}/retry")
    def retry_job(job_id: int, session: DB, _user: User):
        jobs.retry(session, job_id)
        return RedirectResponse("/jobs", status_code=303)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app
