"""Command line: inspect and summarize books, and run the service (migrate, serve, worker)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from chapterwise.cache import FileCache
from chapterwise.config import Settings
from chapterwise.llm import LLMError, OpenAICompatibleLLM
from chapterwise.models import Document
from chapterwise.parsers import ParseError, parse
from chapterwise.summarize import Summarizer, Usage

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Summarize books (EPUB, PDF, Markdown) chapter by chapter with an LLM.",
)

BookPath = Annotated[Path, typer.Argument(help="Path to an .epub, .pdf or .md file")]


def _load(path: Path) -> Document:
    try:
        return parse(path)
    except ParseError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _select(doc: Document, selection: list[str] | None, all_sections: bool) -> list[int]:
    count = len(doc.sections)
    if all_sections:
        return list(range(count))
    if not selection:
        typer.echo("Error: give section numbers (see `chapterwise toc`) or --all", err=True)
        raise typer.Exit(2)

    indices: list[int] = []
    for token in ",".join(selection).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            if "-" in token:
                start, end = (int(x) for x in token.split("-", 1))
                indices.extend(range(start, end + 1))
            else:
                indices.append(int(token))
        except ValueError:
            typer.echo(f"Error: '{token}' is not a section number or range like 2-5", err=True)
            raise typer.Exit(2) from None

    bad = [i for i in indices if not 0 <= i < count]
    if bad:
        typer.echo(f"Error: no section {bad[0]}; this book has sections 0-{count - 1}", err=True)
        raise typer.Exit(2)
    return list(dict.fromkeys(indices))  # unique, in the order given


@app.command()
def toc(path: BookPath) -> None:
    """List the sections a book is split into (no API calls)."""
    doc = _load(path)
    typer.echo(doc.title + (f" — {doc.author}" if doc.author else ""))
    total_words = sum(s.words for s in doc.sections)
    typer.echo(f"{doc.format.upper()} · {len(doc.sections)} sections · {total_words:,} words\n")
    top = min(s.level for s in doc.sections)
    for s in doc.sections:
        indent = "  " * (s.level - top)
        typer.echo(f"{s.index:>4}  {indent}{s.title}  ({s.words:,} words, ~{s.tokens:,} tokens)")


@app.command()
def text(path: BookPath, section: Annotated[int, typer.Argument(help="Section number from `toc`")]) -> None:
    """Print the extracted text of one section (no API calls)."""
    doc = _load(path)
    (index,) = _select(doc, [str(section)], False)
    typer.echo(doc.sections[index].text)


@app.command()
def summarize(
    path: BookPath,
    sections: Annotated[
        list[str] | None, typer.Argument(help="Section numbers or ranges, e.g. 3  or  2-5  or  1,4")
    ] = None,
    all_sections: Annotated[bool, typer.Option("--all", help="Summarize every section")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only show the estimated cost")] = False,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write Markdown here instead of stdout")
    ] = None,
) -> None:
    """Summarize sections with the LLM. Results are cached; repeats cost nothing."""
    settings = Settings.from_env()
    doc = _load(path)
    indices = _select(doc, sections, all_sections)
    cache = FileCache(settings.cache_dir)

    if dry_run:
        estimator = Summarizer(_NoLLM(settings.model), cache, settings.max_chunk_tokens)
        calls = tokens = 0
        for i in indices:
            section = doc.sections[i]
            if estimator.is_cached(doc, section):
                typer.echo(f"{i:>4}  {section.title}: cached, free")
                continue
            est = estimator.estimate(section)
            calls += est.llm_calls
            tokens += est.input_tokens
            typer.echo(
                f"{i:>4}  {section.title}: {est.llm_calls} call(s), ~{est.input_tokens:,} input tokens"
            )
        typer.echo(f"\nTotal: {calls} call(s), ~{tokens:,} input tokens with {settings.model}")
        return

    if not settings.api_key:
        typer.echo("Error: set DEEPSEEK_API_KEY (copy .env.example to .env)", err=True)
        raise typer.Exit(2)

    summarizer = Summarizer(
        OpenAICompatibleLLM(settings.api_key, settings.base_url, settings.model),
        cache,
        settings.max_chunk_tokens,
    )
    total = Usage()
    blocks: list[str] = [f"# {doc.title}" + (f"\n\n*{doc.author}*" if doc.author else "")]
    for i in indices:
        section = doc.sections[i]
        typer.echo(f"Summarizing {i}: {section.title} …", err=True)
        try:
            summary, usage = summarizer.summarize_section(doc, section)
        except LLMError as exc:
            typer.echo(f"Error from the model on section {i}: {exc}", err=True)
            raise typer.Exit(1) from exc
        total += usage
        blocks.append(f"## {section.title}\n\n{summary}")

    markdown = "\n\n".join(blocks) + "\n"
    if out:
        out.write_text(markdown, encoding="utf-8")
        typer.echo(f"Wrote {out}", err=True)
    else:
        typer.echo(markdown)
    typer.echo(
        f"Done: {total.llm_calls} API call(s), {total.cache_hits} from cache, "
        f"{total.tokens_in:,} tokens in / {total.tokens_out:,} out",
        err=True,
    )


# --- service ----------------------------------------------------------------------------------------------


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def migrate() -> None:
    """Create or upgrade the database schema."""
    from chapterwise.migrate import upgrade

    settings = Settings.from_env()
    settings.require("database_url")
    _logging()
    upgrade(settings.database_url)
    typer.echo("Database is up to date.")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8100,
) -> None:
    """Run the web UI."""
    import uvicorn

    from chapterwise.web import create_app

    _logging()
    app_ = create_app(Settings.from_env())
    # Behind Caddy: trust X-Forwarded-* so logs and login throttling see the real client address.
    uvicorn.run(app_, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")


@app.command()
def worker() -> None:
    """Run the background worker: library scans, parsing and summaries."""
    from chapterwise.db import make_engine, make_session_factory
    from chapterwise.worker import Worker

    settings = Settings.from_env()
    settings.require("database_url")
    _logging()
    Worker(settings, make_session_factory(make_engine(settings.database_url))).run_forever()


class _NoLLM:
    """Stand-in for --dry-run: knows the model name for cache keys, refuses to be called."""

    def __init__(self, model: str):
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def complete(self, system: str, user: str):
        raise RuntimeError("dry run must not call the model")
