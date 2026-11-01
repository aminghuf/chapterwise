from chapterwise.telegram_format import build_message, markdown_to_blocks, to_plain_text


def test_converts_markdown_to_telegram_html():
    md = """Overview with **bold**, *italic*, `code` and a [link](https://example.com).

## Key ideas

- First **idea**
- Second idea
  - nested point

1. one
2. two

**Takeaway:** keep going."""
    blocks = markdown_to_blocks(md)
    assert blocks[0] == (
        'Overview with <b>bold</b>, <i>italic</i>, <code>code</code> and a <a href="https://example.com">link</a>.'
    )
    assert blocks[1] == "<b>Key ideas</b>"
    assert blocks[2] == "• First <b>idea</b>\n• Second idea\n  • nested point"
    assert blocks[3] == "1. one\n2. two"
    assert blocks[4] == "<b>Takeaway:</b> keep going."


def test_escapes_html_and_drops_unsafe_links():
    blocks = markdown_to_blocks("Use <script>alert(1)</script> & a [bad](javascript:alert(1)) link, 3 < 4")
    html = " ".join(blocks)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&amp;" in html and "3 &lt; 4" in html
    assert "href" not in html  # javascript: links are never turned into links


def test_build_message_fits_and_cuts_at_block_boundary():
    md = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(30))
    html, truncated = build_message("<b>Book</b>", md, budget=1000)
    assert truncated
    assert len(html) <= 1000
    assert html.endswith("…")
    assert html.count("<b>") == html.count("</b>")

    short, truncated = build_message("<b>Book</b>", "Just one line.")
    assert not truncated and short == "<b>Book</b>\n\nJust one line."


def test_plain_text_fallback():
    assert to_plain_text("<b>Book</b>\n\n• one &amp; two") == "Book\n\n• one & two"


def test_card_message():
    from chapterwise.summary_format import parse_card
    from chapterwise.telegram_format import build_card_message
    from tests.conftest import card_json

    card = parse_card(card_json(tldr="A <tag> & **bold** line."))
    html, truncated = build_card_message("<b>Book</b>", card)
    assert not truncated
    assert "💡 <b>TL;DR</b>\nA &lt;tag&gt; &amp; <b>bold</b> line." in html
    assert "⏱️ <b>Short sprints work</b>\nWork in <b>25-minute</b> blocks." in html
    assert "✅ <b>Try this:</b> Set one timer today." in html

    long_card = parse_card(card_json(key_ideas=[{"title": f"T{n}", "point": "x" * 590} for n in range(8)]))
    html, truncated = build_card_message("<b>Book</b>", long_card)
    assert truncated and len(html) <= 3800 and html.endswith("(open the article for the rest)")
