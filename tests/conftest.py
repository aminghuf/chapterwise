"""Fixtures: small books generated on the fly, and a fake LLM that records its calls."""

from __future__ import annotations

from pathlib import Path

import pytest
from ebooklib import epub

from chapterwise.llm import Completion


def words(label: str, n: int = 60) -> str:
    """n distinct-ish words, tagged with a label so tests can tell sections apart."""
    return " ".join(f"{label}{i}" for i in range(n))


def _page(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


@pytest.fixture
def sample_epub(tmp_path: Path) -> Path:
    """Two parts with three chapters; chapters 1 and 2 share one file and are found by anchors."""
    book = epub.EpubBook()
    book.set_identifier("test-book")
    book.set_title("The Test Book")
    book.set_language("en")
    book.add_author("Ada Tester")

    part1 = epub.EpubHtml(title="Part I", file_name="text/part1.xhtml", lang="en")
    part1.content = _page(
        "Part I",
        f"<h1 id='ch1'>Chapter 1</h1><p>{words('alpha')}</p>"
        f"<p>Second <em>paragraph</em> of chapter one.<br/>New line.</p>"
        f"<h1 id='ch2'>Chapter 2</h1><p>{words('beta')}</p>",
    )
    part2 = epub.EpubHtml(title="Part II", file_name="text/part2.xhtml", lang="en")
    part2.content = _page("Part II", f"<h1>Chapter 3</h1><p>{words('gamma')}</p>")
    notes = epub.EpubHtml(title="Notes", file_name="text/notes.xhtml", lang="en")
    notes.content = _page("Notes", f"<p>{words('footnote')}</p>")
    for item in (part1, part2, notes):
        book.add_item(item)

    book.toc = (
        (
            epub.Section("Part I", href="text/part1.xhtml"),
            (
                epub.Link("text/part1.xhtml#ch1", "Chapter 1", "c1"),
                epub.Link("text/part1.xhtml#ch2", "Chapter 2", "c2"),
            ),
        ),
        (
            epub.Section("Part II", href="text/part2.xhtml"),
            (epub.Link("text/part2.xhtml", "Chapter 3", "c3"),),
        ),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", part1, part2, (notes.id, "no")]

    path = tmp_path / "test-book.epub"
    epub.write_epub(str(path), book)
    return path


@pytest.fixture
def make_pdf(tmp_path: Path):
    """Build a PDF: pages is a list of page texts; outline is [(title, level, page index), ...]."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    def build(pages: list[str], outline: list[tuple[str, int, int]] | None = None, name="book.pdf") -> Path:
        path = tmp_path / name
        c = canvas.Canvas(str(path), pagesize=A4)
        c.setTitle("PDF Book")
        c.setAuthor("Pat Writer")
        starts = {}
        for title, level, page in outline or []:
            starts.setdefault(page, []).append((title, level))
        for index, page_text in enumerate(pages):
            for n, (title, level) in enumerate(starts.get(index, [])):
                key = f"p{index}-{n}"
                c.bookmarkPage(key)
                c.addOutlineEntry(title, key, level=level - 1)
            y = 800
            for line in _wrap(page_text, 12):
                c.drawString(40, y, line)
                y -= 14
            c.showPage()
        c.save()
        return path

    return build


def _wrap(text: str, words_per_line: int) -> list[str]:
    tokens = text.split()
    return [" ".join(tokens[i : i + words_per_line]) for i in range(0, len(tokens), words_per_line)]


def card_json(tldr: str = "The chapter in one line.", **overrides) -> str:
    """A valid summary card, as the model would return it."""
    import json

    card = {
        "tldr": tldr,
        "why_it_matters": "It changes how you plan your day.",
        "key_ideas": [
            {
                "emoji": "🧠",
                "title": "Attention is limited",
                "point": "You **can't** focus on everything.",
                "example": "Like a phone battery that drains.",
            },
            {
                "emoji": "⏱️",
                "title": "Short sprints work",
                "point": "Work in **25-minute** blocks.",
                "example": "Set a kitchen timer.",
            },
        ],
        "terms": [{"term": "Pomodoro", "meaning": "a 25-minute focused work block"}],
        "takeaway": "Protect your focus like money.",
        "try_this": "Set one timer today.",
        "quick_check": [{"question": "How long is a sprint?", "answer": "25 minutes."}],
    }
    card.update(overrides)
    return json.dumps(card, ensure_ascii=False)


class FakeLLM:
    """Returns predictable text and records every prompt it receives."""

    def __init__(self, model: str = "fake-model"):
        self._model = model
        self.calls: list[str] = []
        self.json_modes: list[bool] = []

    @property
    def model(self) -> str:
        return self._model

    def complete(self, system: str, user: str, json_mode: bool = False) -> Completion:
        self.calls.append(user)
        self.json_modes.append(json_mode)
        if "(part " in user:
            kind = "notes"
        elif "<notes>" in user and "Condense" in user:
            kind = "condensed"
        elif "<notes>" in user:
            kind = "merged summary"
        else:
            kind = "summary"
        text = f"{kind} #{len(self.calls)}"
        if json_mode:  # final summaries are JSON cards; the label goes in the TL;DR
            text = card_json(tldr=text)
        return Completion(text=text, tokens_in=len(user) // 4, tokens_out=10)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


# --- Postgres (service tests) -----------------------------------------------------------------------------
# Set TEST_DATABASE_URL to a database the tests may wipe, e.g.
#   postgresql+psycopg://postgres:postgres@localhost:5432/chapterwise_test
# Tests that need it are skipped when it isn't set.


@pytest.fixture(scope="session")
def database_url() -> str:
    import os

    from chapterwise.config import normalize_database_url

    url = normalize_database_url(os.environ.get("TEST_DATABASE_URL"))
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
def migrated_engine(database_url):
    from sqlalchemy import text

    from chapterwise.db import make_engine
    from chapterwise.migrate import upgrade

    engine = make_engine(database_url)
    with engine.begin() as conn:  # start from an empty schema so the migration itself is tested
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    upgrade(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(migrated_engine):
    from sqlalchemy import text

    from chapterwise.db import make_session_factory

    with migrated_engine.begin() as conn:
        conn.execute(text("TRUNCATE documents, sections, jobs, llm_cache RESTART IDENTITY CASCADE"))
    return make_session_factory(migrated_engine)


@pytest.fixture
def books_dir(tmp_path: Path, sample_epub: Path) -> Path:
    """A books folder laid out like BookOrbit's: Author/Title/file."""
    folder = tmp_path / "books"
    (folder / "Ada Tester" / "The Test Book").mkdir(parents=True)
    sample_epub.rename(folder / "Ada Tester" / "The Test Book" / "test-book.epub")
    (folder / "notes").mkdir()
    (folder / "notes" / "playbook.md").write_text(
        f"# Playbook\n\n## One\n\n{words('one')}\n\n## Two\n\n{words('two')}\n", encoding="utf-8"
    )
    (folder / "notes" / "ignored.txt").write_text("not a book")
    (folder / ".hidden").mkdir()
    (folder / ".hidden" / "secret.md").write_text(words("hidden"))
    return folder


@pytest.fixture
def settings(database_url, books_dir):
    from chapterwise.config import Settings

    return Settings(
        api_key="sk-test",
        base_url="http://127.0.0.1:9",
        model="fake-model",
        max_chunk_tokens=24_000,
        cache_dir=Path("/nonexistent"),
        database_url=database_url,
        books_dir=books_dir,
        secret_key="test-secret",
        admin_password="correct horse",
        public_url="http://testserver",
        scan_interval=600,
    )
