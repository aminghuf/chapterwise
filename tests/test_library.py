import os

from sqlalchemy import select

from chapterwise.db import DocumentRow, Job, SectionRow
from chapterwise.library import find_books, parse_document, scan


def test_find_books_skips_hidden_and_unsupported(books_dir):
    assert sorted(find_books(books_dir)) == ["Ada Tester/The Test Book/test-book.epub", "notes/playbook.md"]


def test_scan_registers_new_files_and_queues_parsing(sessions, books_dir):
    with sessions() as s:
        result = scan(s, books_dir)
        assert (result.added, result.changed, result.missing) == (2, 0, 0)
        docs = s.scalars(select(DocumentRow).order_by(DocumentRow.path)).all()
        assert [d.status for d in docs] == ["pending", "pending"]
        assert docs[0].format == "epub" and docs[1].format == "md"
        assert sorted(j.dedupe_key for j in s.scalars(select(Job))) == ["parse:1", "parse:2"]

        again = scan(s, books_dir)
        assert (again.added, again.changed, again.unchanged) == (0, 0, 2)


def test_scan_detects_changes_and_removals(sessions, books_dir):
    with sessions() as s:
        scan(s, books_dir)
        playbook = books_dir / "notes" / "playbook.md"
        playbook.write_text(playbook.read_text() + "\nmore text\n")
        os.utime(playbook, (1_000_000_000, 1_000_000_000))
        (books_dir / "Ada Tester" / "The Test Book" / "test-book.epub").unlink()

        result = scan(s, books_dir)
        assert (result.changed, result.missing) == (1, 1)
        statuses = {d.path: d.status for d in s.scalars(select(DocumentRow))}
        assert statuses == {
            "notes/playbook.md": "pending",
            "Ada Tester/The Test Book/test-book.epub": "missing",
        }


def test_parse_document_stores_sections(sessions, books_dir):
    with sessions() as s:
        scan(s, books_dir)
        epub = s.scalar(select(DocumentRow).where(DocumentRow.format == "epub"))
        parse_document(s, books_dir, epub.id)

        s.refresh(epub)
        assert (epub.status, epub.title, epub.author) == ("ready", "The Test Book", "Ada Tester")
        assert len(epub.sha256) == 64
        sections = s.scalars(select(SectionRow).order_by(SectionRow.position)).all()
        assert [x.title for x in sections] == ["Chapter 1", "Chapter 2", "Chapter 3"]
        assert all(len(x.text_sha256) == 64 and x.words >= 20 for x in sections)

        parse_document(s, books_dir, epub.id)  # re-parsing replaces, never duplicates
        assert len(s.scalars(select(SectionRow)).all()) == 3


def test_unreadable_file_is_marked_error(sessions, books_dir):
    (books_dir / "broken.pdf").write_bytes(b"not really a pdf")
    with sessions() as s:
        scan(s, books_dir)
        broken = s.scalar(select(DocumentRow).where(DocumentRow.path == "broken.pdf"))
        parse_document(s, books_dir, broken.id)
        s.refresh(broken)
        assert broken.status == "error" and broken.error
