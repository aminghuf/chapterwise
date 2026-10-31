# chapterwise

Summarize your books chapter by chapter with an LLM, then read them in micro-doses.

chapterwise splits a book into sections using its own table of contents, summarizes a chapter
(or the whole book) with DeepSeek or any OpenAI-compatible API, and caches every result so the
same request is never paid for twice. It's built to run next to a self-hosted library such as
[BookOrbit](https://github.com/bookorbit/bookorbit), reading the same books folder read-only.

**Status:** Phase 3. A command-line tool, a self-hosted web app (library, chapters, summaries) with a
background worker, and a personal Telegram bot that sends one summarized section every morning.
Deployed by CI/CD (see [Roadmap](#roadmap)).

```
books folder (read-only) ──► worker: scan · parse · summarize ──► DeepSeek API
                                     │
                                  Postgres  (books, sections, job queue, LLM cache)
                                     │
                 browser ──► Caddy ──► web: login · library · book · section
                                     │
                     Telegram ◄──── bot: daily delivery · /books · /reading · /next
```

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

## The service

Three processes from one image or install, sharing one Postgres database:

| Command | Role |
| --- | --- |
| `chapterwise migrate` | Create or upgrade the schema (Alembic) |
| `chapterwise serve` | Web UI on port 8100 |
| `chapterwise worker` | Scans the books folder every `CHAPTERWISE_SCAN_INTERVAL` seconds, parses new or changed files, runs summaries |
| `chapterwise bot` | Telegram bot (long polling, so no open port): daily delivery and commands |

- **Job queue:** a Postgres table claimed with `FOR UPDATE SKIP LOCKED`, so there's no Redis to run.
- **Retries:** failed jobs retry with exponential backoff, up to 3 attempts. Jobs interrupted by a
  restart are requeued.
- **Summaries are cached** in Postgres, keyed like the CLI cache.
- **Web UI:** server-rendered with no JavaScript. It refreshes itself while work is queued.
- **Security:** single password with login throttling, `SameSite` session cookie plus an Origin
  check on every form post, strict Content-Security-Policy. Model output is rendered as Markdown
  with raw HTML escaped.

Run it locally against any Postgres:

```bash
cp .env.example .env          # set DATABASE_URL, CHAPTERWISE_BOOKS_DIR, secrets
chapterwise migrate
chapterwise serve & chapterwise worker
```

## Daily reading on Telegram

Pick a book with `/books` (or **Read daily on Telegram** on its web page). Every morning at
`CHAPTERWISE_DELIVERY_TIME` (07:30 Europe/Rome by default) the bot sends the next section's summary,
with buttons to get the **Next** one right away, **Pause**, or open it on the web.

- **Summaries are prepared ahead:** the next 2 sections are queued for the worker, so the morning
  message is ready on time.
- **Missed deliveries catch up:** if the bot was down at 07:30, it sends that day's section on its
  first check after coming back. Each section is sent once.
- **Next** sends another section now, and tomorrow's delivery still comes. If that summary isn't
  ready yet, it arrives as soon as it is.
- **Commands:** `/books [word]` picks a book, `/reading` shows progress with Pause, Resume and Stop,
  and `/next` sends the next section now.
- **Private:** only `TELEGRAM_CHAT_ID` is answered. Until it's set, `/start` replies with your chat
  ID to put there.
- **Formatting:** summaries use Telegram's HTML subset, so lists become bullet lines and long
  summaries are cut at a paragraph boundary. If Telegram ever rejects the formatting, the message
  is resent as plain text.

## Deployment

Pushes to `main` run the tests, build the image to `ghcr.io/aminghuf/chapterwise:sha-<commit>`, then
deploy over SSH to the VPS. The deploy writes the image tag, backs up the database, runs
`docker compose up` (migrations run first) and waits for `/healthz`. Files are in [`deploy/`](deploy).

**One-time setup:**

1. **DNS:** `A read → <server IP>`.
2. **Server:** copy `deploy/server-bootstrap.sh` to the VPS and run `bash server-bootstrap.sh`. It
   creates `/root/chapterwise/.env` with generated secrets and asks for the web password, the API key
   and the Telegram bot token and chat ID. Re-running it only adds what's missing.
3. **Caddy:** add `deploy/read.aminghuf.com.caddy` to the Caddyfile and reload Caddy.
4. **GitHub secrets** (Settings → Secrets and variables → Actions):
   - `DEPLOY_SSH_KEY`: a private key allowed to SSH in as root
   - `DEPLOY_HOST`: the server's IP
   - `DEPLOY_KNOWN_HOSTS`: the server's host-key line, from `ssh-keyscan -t ed25519 <ip>` checked
     against the server
   - Optional variable `DEPLOY_DIR`: the server folder to deploy to (default `/root/chapterwise`)

**On the server:** `cd /root/chapterwise && ./cw.sh ps | logs -f worker | backup`.

## Configuration

Set these in `.env` or the environment:

| Variable | Default | |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | | Required for `summarize` (not for `--dry-run`) |
| `CHAPTERWISE_BASE_URL` | `https://api.deepseek.com` | Any OpenAI-compatible endpoint |
| `CHAPTERWISE_MODEL` | `deepseek-flash` | |
| `CHAPTERWISE_CACHE_DIR` | `.cache/chapterwise` | |
| `CHAPTERWISE_MAX_CHUNK_TOKENS` | `24000` | Approximate tokens per call |
| `DATABASE_URL` | | Service only; any `postgresql://` URL |
| `CHAPTERWISE_BOOKS_DIR` | `/books` | Folder scanned for books (mounted read-only in Docker) |
| `CHAPTERWISE_SECRET_KEY` | | Signs the login cookie |
| `CHAPTERWISE_ADMIN_PASSWORD` | | The web UI password |
| `CHAPTERWISE_PUBLIC_URL` | `http://localhost:8100` | With `https://`, cookies are marked Secure |
| `CHAPTERWISE_SCAN_INTERVAL` | `600` | Seconds between library scans |
| `TELEGRAM_BOT_TOKEN` | | From @BotFather; the bot idles without it |
| `TELEGRAM_CHAT_ID` | | The only chat the bot answers |
| `CHAPTERWISE_DELIVERY_TIME` | `07:30` | Daily delivery, local time |
| `CHAPTERWISE_TIMEZONE` | `Europe/Rome` | Time zone for the delivery time |

## Development

```bash
pytest            # tests build their own EPUB/PDF/Markdown fixtures, no network or API key needed

# Service tests need a Postgres they may wipe (skipped otherwise):
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/chapterwise_test pytest
ruff check . && ruff format .
```

## Roadmap

- [x] **Phase 1:** parsers, chunking, cached map-reduce summaries, CLI
- [x] **Phase 2:** Postgres, background worker, books-folder sync, web UI at read.aminghuf.com, CI/CD to the VPS
- [x] **Phase 3:** personal Telegram bot with scheduled delivery
- [ ] **Phase 4:** whole-book summaries, micro-doses (original text plus recall questions), Markdown playbooks as a source

## Notes

Book text is sent to the configured LLM provider. It's meant for personal use with books you own.

## License

[MIT](LICENSE)
