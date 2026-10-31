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


class FakeLLM:
    """Returns predictable text and records every prompt it receives."""

    def __init__(self, model: str = "fake-model"):
        self._model = model
        self.calls: list[str] = []

    @property
    def model(self) -> str:
        return self._model

    def complete(self, system: str, user: str) -> Completion:
        self.calls.append(user)
        if "(part " in user:
            kind = "notes"
        elif "<notes>" in user and "Condense" in user:
            kind = "condensed"
        elif "<notes>" in user:
            kind = "merged summary"
        else:
            kind = "summary"
        return Completion(text=f"{kind} #{len(self.calls)}", tokens_in=len(user) // 4, tokens_out=10)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
