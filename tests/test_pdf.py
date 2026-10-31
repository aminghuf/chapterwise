import pytest

from chapterwise.parsers import ParseError, parse
from chapterwise.parsers.pdf import PAGES_PER_SECTION
from tests.conftest import words


def test_sections_from_outline(make_pdf):
    pages = [words("intro", 30), words("one"), words("onemore"), words("two"), words("three")]
    outline = [("Chapter One", 1, 1), ("A subsection", 2, 2), ("Chapter Two", 1, 3), ("Chapter Three", 1, 4)]
    doc = parse(make_pdf(pages, outline))

    assert doc.title == "PDF Book"
    assert doc.author == "Pat Writer"
    assert [s.title for s in doc.sections] == ["Chapter One", "Chapter Two", "Chapter Three"]
    one = doc.sections[0].text
    assert "one0" in one and "onemore0" in one  # the subsection's page belongs to its chapter
    assert "two0" not in one
    assert "intro0" not in one  # short front matter is dropped


def test_no_outline_uses_page_ranges(make_pdf):
    pages = [words(f"page{i}x", 25) for i in range(PAGES_PER_SECTION + 3)]
    doc = parse(make_pdf(pages))
    assert [s.title for s in doc.sections] == [
        f"Pages 1-{PAGES_PER_SECTION}",
        f"Pages {PAGES_PER_SECTION + 1}-13",
    ]
    assert "page12x0" in doc.sections[1].text


def test_pdf_without_text_is_rejected(make_pdf):
    with pytest.raises(ParseError, match="no extractable text"):
        parse(make_pdf(["", ""]))
