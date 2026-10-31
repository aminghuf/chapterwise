# chapterwise

Summarize your books chapter by chapter with an LLM, then read them in micro-doses.

chapterwise splits a book into sections using its own table of contents, summarizes a chapter
(or the whole book) with DeepSeek or any OpenAI-compatible API, and caches every result so the
same request is never paid for twice. It's built to run next to a self-hosted library such as
[BookOrbit](https://github.com/bookorbit/bookorbit), reading the same books folder read-only.

**Status:** Phase 1, a command-line tool. The web UI, Telegram delivery and scheduling come next
(see [Roadmap](#roadmap)).

## Supported formats

| Format | Sections come from | Without a table of contents |
| --- | --- | --- |
| EPUB | The book's TOC (NCX or EPUB 3 nav), including chapters that share a file | One section per file |
| PDF | The PDF outline (bookmarks) | 10-page ranges |
| Markdown | Headings (a single `# Title` is the document title) | The whole file |

Scanned PDFs (images only) have no text to extract and are rejected; OCR isn't supported yet.

## Quick start

```bash
git clone https://github.com/aminghuf/chapterwise.git
cd chapterwise
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env    # then set DEEPSEEK_API_KEY
```

```bash
chapterwise toc book.epub                    # list sections (no API calls)
chapterwise text book.epub 3                 # print the extracted text of section 3
chapterwise summarize book.epub 3 --dry-run  # estimated calls and tokens, no API calls
chapterwise summarize book.epub 3            # summarize section 3
chapterwise summarize book.epub 2-5 -o notes.md
chapterwise summarize book.epub --all
```

Summaries are written in the same language as the book.

## How summarizing works

1. **Parse** the file into sections. No LLM is involved, so this is free and instant.
2. **Fit check:** a section that fits in `CHAPTERWISE_MAX_CHUNK_TOKENS` is summarized in one call.
3. **Longer sections** are split at paragraph, then sentence, boundaries. Each part is turned
   into notes, and the notes are merged into the final summary (condensed first if they're
   still too long).
4. **Caching:** every call is cached by the text's hash, the model and the prompt version. Repeats
   are free, an interrupted run resumes where it stopped, and changing the model or a prompt
   regenerates only what's affected.

## Configuration

Set these in `.env` or the environment:

| Variable | Default | |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | | Required for `summarize` (not for `--dry-run`) |
| `CHAPTERWISE_BASE_URL` | `https://api.deepseek.com` | Any OpenAI-compatible endpoint |
| `CHAPTERWISE_MODEL` | `deepseek-flash` | |
| `CHAPTERWISE_CACHE_DIR` | `.cache/chapterwise` | |
| `CHAPTERWISE_MAX_CHUNK_TOKENS` | `24000` | Approximate tokens per call |

## Development

```bash
pytest            # tests build their own EPUB/PDF/Markdown fixtures, no network or API key needed
ruff check . && ruff format .
```

## Roadmap

- [x] **Phase 1:** parsers, chunking, cached map-reduce summaries, CLI
- [ ] **Phase 2:** Postgres, background worker, books-folder sync, web UI at read.aminghuf.com, CI/CD to the VPS
- [ ] **Phase 3:** personal Telegram bot with scheduled delivery
- [ ] **Phase 4:** whole-book summaries, micro-doses (original text plus recall questions), Markdown playbooks as a source

## Notes

Book text is sent to the configured LLM provider. It's meant for personal use with books you own.

## License

[MIT](LICENSE)
