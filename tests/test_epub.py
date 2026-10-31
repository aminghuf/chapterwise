from chapterwise.parsers import parse


def test_metadata(sample_epub):
    doc = parse(sample_epub)
    assert doc.format == "epub"
    assert doc.title == "The Test Book"
    assert doc.author == "Ada Tester"
    assert doc.language == "en"
    assert len(doc.sha256) == 64


def test_splits_at_chapters_including_shared_files(sample_epub):
    doc = parse(sample_epub)
    # Only two top-level entries (the parts), so the parser goes one level deeper to the chapters.
    assert [s.title for s in doc.sections] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert [s.index for s in doc.sections] == [0, 1, 2]

    ch1, ch2, ch3 = (s.text for s in doc.sections)
    assert "alpha0" in ch1 and "alpha59" in ch1 and "beta0" not in ch1
    assert "beta0" in ch2 and "alpha0" not in ch2 and "gamma0" not in ch2
    assert "gamma0" in ch3


def test_text_is_clean(sample_epub):
    ch1 = parse(sample_epub).sections[0].text
    assert "Second paragraph of chapter one.\nNew line." in ch1  # inline tags don't break sentences
    assert "\n\n\n" not in ch1
    assert "<" not in ch1


def test_skips_non_linear_items_and_navigation(sample_epub):
    all_text = " ".join(s.text for s in parse(sample_epub).sections)
    assert "footnote0" not in all_text  # non-linear notes file
    assert "Table of Contents" not in all_text  # nav page isn't a chapter
