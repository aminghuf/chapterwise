"""The web UI: a single-user, server-rendered app. JavaScript only enhances the summary article."""
# No `from __future__ import annotations` here: FastAPI must see the real types of the local DB/User aliases.

import hmac
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape
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
from chapterwise.links import read_token_target, read_url
from chapterwise.summarize import StoredSummary, find_summary, summary_keys
from chapterwise.summary_format import try_parse_card

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
SESSION_MAX_AGE = 30 * 24 * 3600

# Raw HTML in model output is escaped, never rendered.
_markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors {}"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP.format("'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
# Read links open inside Telegram; Telegram Web shows mini apps in an iframe.
READ_LINK_HEADERS = {
    "Content-Security-Policy": _CSP.format("https://web.telegram.org"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def emphasize(text: str) -> Markup:
    """Escape model text, then turn its **bold** markers into <strong>."""
    return Markup(_BOLD.sub(r"<strong>\1</strong>", str(escape(text or ""))))


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
    templates.env.filters["emph"] = emphasize

    app = FastAPI(title="chapterwise", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="chapterwise_session",
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def protect(request: Request, call_next):
        # Reject cross-site form posts (on top of SameSite cookies).
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and urlparse(source).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-site request blocked", status_code=403)
        response = await call_next(request)
        headers = READ_LINK_HEADERS if request.url.path.startswith("/r/") else SECURITY_HEADERS
        for name, value in headers.items():
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

    def summary_digests(doc_title: str, section_title: str, text_hash: str) -> list[str]:
        """Digests a section's summary may be stored under: current format first, then older ones."""
        return [cache_digest(k) for k in summary_keys(text_hash, doc_title, section_title, settings.model)]

    def summary_states(doc: DocumentRow, sections) -> dict[int, str]:
        """position -> "done" (current format) | "legacy" (older format) | "none"."""
        digests = {s.position: summary_digests(doc.title, s.title, s.text_sha256) for s in sections}
        found = cache.existing([d for ds in digests.values() for d in ds])
        states = {}
        for position, (current, *older) in digests.items():
            if current in found:
                states[position] = "done"
            elif any(d in found for d in older):
                states[position] = "legacy"
            else:
                states[position] = "none"
        return states

    def stored_summary(doc: DocumentRow, section: SectionRow) -> StoredSummary | None:
        return find_summary(cache, section.text_sha256, doc.title, section.title, settings.model)

    def article_context(doc: DocumentRow, section: SectionRow) -> dict:
        stored = stored_summary(doc, section)
        card = try_parse_card(stored.text) if stored and stored.format == "card" else None
        return {
            "doc": doc,
            "section": section,
            "total": len(doc.sections),
            "card": card,
            "legacy": stored.text if stored and card is None else None,
            "is_current": bool(stored and stored.current and card),
            "article_key": f"{doc.id}-{section.position}-{section.text_sha256[:10]}",
        }

    def neighbours(doc: DocumentRow, position: int) -> tuple[SectionRow | None, SectionRow | None]:
        ordered = sorted(doc.sections, key=lambda s: s.position)
        index = next(i for i, s in enumerate(ordered) if s.position == position)
        return (
            ordered[index - 1] if index > 0 else None,
            ordered[index + 1] if index + 1 < len(ordered) else None,
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
        digests: dict[int, list[list[str]]] = defaultdict(list)
        for document_id, title, sha in sections:
            digests[document_id].append(summary_digests(by_id[document_id].title, title, sha))
        found = cache.existing([d for per_doc in digests.values() for ds in per_doc for d in ds])
        stats = {
            doc_id: {
                "sections": len(per_doc),
                "summarized": sum(1 for ds in per_doc if any(d in found for d in ds)),
            }
            for doc_id, per_doc in digests.items()
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
        states = summary_states(doc, sections)
        active = active_summaries(session, doc.id)
        rows = [{"section": s, "state": active.get(s.position) or states[s.position]} for s in sections]
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
            legacy_count=sum(1 for r in rows if r["state"] == "legacy"),
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
        if sections in ("all", "legacy"):
            wanted = "none" if sections == "all" else "legacy"
            states = summary_states(doc, doc.sections)
            positions = [s.position for s in doc.sections if states[s.position] == wanted]
            if not positions:
                return RedirectResponse(f"/b/{doc.id}", status_code=303)
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
        section = next((s for s in doc.sections if s.position == position), None)
        if section is None:
            raise HTTPException(404, "Section not found")
        context = article_context(doc, section)
        state = active_summaries(session, doc.id).get(position)
        if state is None:
            state = "done" if context["card"] or context["legacy"] else "none"
        prev, next_ = neighbours(doc, position)
        return render(
            request,
            "section.html",
            **context,
            state=state,
            prev=prev,
            next=next_,
            prev_href=f"/b/{doc.id}/s/{prev.position}" if prev else None,
            next_href=f"/b/{doc.id}/s/{next_.position}" if next_ else None,
            back_href=f"/b/{doc.id}#s{position}",
            summarize_form=True,
            upgrade_form=not context["is_current"],
            refresh=state in ACTIVE_JOB_STATUSES and not context["card"],
        )

    # --- signed read links (opened from Telegram, no login) ----------------------------------------------

    @app.get("/r/{token}", response_class=HTMLResponse)
    def read_link(request: Request, token: str, session: DB):
        target = read_token_target(settings.secret_key, token)
        doc = session.get(DocumentRow, target[0]) if target else None
        section = next((s for s in doc.sections if s.position == target[1]), None) if doc else None
        if section is None:
            return HTMLResponse(
                "<!doctype html><meta name=viewport content='width=device-width'>"
                "<p style='font:16px system-ui;padding:24px'>This link has expired or no longer exists.</p>",
                status_code=404,
            )
        prev, next_ = neighbours(doc, section.position)

        def link(s: SectionRow | None) -> str | None:
            return read_url("", settings.secret_key, doc.id, s.position) if s else None

        return render(
            request,
            "reader.html",
            **article_context(doc, section),
            state="none",
            prev=prev,
            next=next_,
            prev_href=link(prev),
            next_href=link(next_),
            back_href=None,
            summarize_form=False,
            upgrade_form=False,
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
