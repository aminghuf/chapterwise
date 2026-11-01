"""Turn summaries (cards or Markdown) into the small HTML subset Telegram accepts.

Telegram supports only <b> <i> <u> <s> <code> <pre> <a> <blockquote>, no lists or headings, and caps a
message at 4096 characters. Lists become bullet lines, headings become bold lines, and long summaries
are cut at a block boundary so no tag is ever left open.
"""

from __future__ import annotations

import re
from html import escape

from bs4 import BeautifulSoup, NavigableString, Tag
from markdown_it import MarkdownIt

from chapterwise.summary_format import Card, strip_bold

MESSAGE_BUDGET = 3800  # leaves room under Telegram's 4096-character limit

_markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
_INLINE = {"strong": "b", "b": "b", "em": "i", "i": "i", "del": "s", "s": "s", "u": "u"}


def markdown_to_blocks(markdown: str) -> list[str]:
    """Telegram-HTML blocks (paragraphs, list items, ...) in reading order."""
    soup = BeautifulSoup(_markdown.render(markdown), "html.parser")
    blocks: list[str] = []
    for node in soup.children:
        if isinstance(node, Tag):
            blocks.extend(_block(node))
        elif str(node).strip():
            blocks.append(escape(str(node).strip(), quote=False))
    return [b for b in blocks if b.strip()]


def _block(tag: Tag, depth: int = 0) -> list[str]:
    name = tag.name
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return [f"<b>{_inline(tag)}</b>"]
    if name in ("ul", "ol"):
        lines: list[str] = []
        for number, item in enumerate(tag.find_all("li", recursive=False), start=1):
            marker = f"{number}." if name == "ol" else "•"
            nested = [
                child for child in item.children if isinstance(child, Tag) and child.name in ("ul", "ol")
            ]
            for child in nested:
                child.extract()
            lines.append(f"{'  ' * depth}{marker} {_inline(item).strip()}")
            for child in nested:
                lines.extend(_block(child, depth + 1))
        return ["\n".join(lines)] if depth == 0 else lines
    if name == "pre":
        return [f"<pre>{escape(tag.get_text(), quote=False)}</pre>"]
    if name == "blockquote":
        return [f"<blockquote>{_inline(tag).strip()}</blockquote>"]
    if name == "hr":
        return ["———"]
    if name == "table":
        rows = [
            " | ".join(_inline(cell).strip() for cell in row.find_all(["th", "td"]))
            for row in tag.find_all("tr")
        ]
        return ["\n".join(rows)]
    return [_inline(tag).strip()]


def _inline(node) -> str:
    out: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(escape(str(child), quote=False))
            continue
        name = child.name
        if name in _INLINE:
            tag = _INLINE[name]
            out.append(f"<{tag}>{_inline(child)}</{tag}>")
        elif name == "code":
            out.append(f"<code>{escape(child.get_text(), quote=False)}</code>")
        elif name == "a":
            href = child.get("href", "")
            text = _inline(child)
            out.append(
                f'<a href="{escape(href)}">{text}</a>' if href.startswith(("http://", "https://")) else text
            )
        elif name == "br":
            out.append("\n")
        elif name == "p":
            out.append(_inline(child) + " ")
        else:
            out.append(_inline(child))
    return "".join(out).replace("\n ", "\n")


def build_message(header: str, markdown: str, budget: int = MESSAGE_BUDGET) -> tuple[str, bool]:
    """Header (already HTML) + summary, cut at a block boundary to fit. Returns (html, truncated)."""
    parts = [header]
    used = len(header)
    for block in markdown_to_blocks(markdown):
        if used + len(block) + 2 > budget:
            parts.append("…")
            return "\n\n".join(parts), True
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts), False


def to_plain_text(html: str) -> str:
    """Fallback if Telegram ever rejects the HTML: same content without markup."""
    return BeautifulSoup(html, "html.parser").get_text()


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _tg(text: str) -> str:
    """Escape model text for Telegram HTML and keep its **bold** markers as <b>."""
    return _BOLD.sub(r"<b>\1</b>", escape(text or "", quote=False))


def card_blocks(card: Card) -> list[str]:
    """A short, scannable Telegram version of a summary card; the full article is one tap away."""
    blocks = [f"💡 <b>TL;DR</b>\n{_tg(card.tldr)}"]
    ideas = [
        f"{idea.emoji or '•'} <b>{_tg(strip_bold(idea.title))}</b>\n{_tg(idea.point)}" for idea in card.ideas
    ]
    blocks.append("<b>Key ideas</b>\n\n" + "\n\n".join(ideas))
    if card.takeaway:
        blocks.append(f"🎯 <b>Takeaway:</b> {_tg(card.takeaway)}")
    if card.action:
        blocks.append(f"✅ <b>Try this:</b> {_tg(card.action)}")
    return blocks


def build_card_message(header: str, card: Card, budget: int = MESSAGE_BUDGET) -> tuple[str, bool]:
    """Header (already HTML) + card blocks, cut at a block boundary to fit. Returns (html, truncated)."""
    parts = [header]
    used = len(header)
    for block in card_blocks(card):
        if used + len(block) + 2 > budget:
            parts.append("… (open the article for the rest)")
            return "\n\n".join(parts), True
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts), False
