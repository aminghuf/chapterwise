"""The bot end to end: real python-telegram-bot, real HTTP, against a local fake of the Bot API."""

import asyncio
import json
import threading
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from telegram import Update

from chapterwise import delivery
from chapterwise.bot import ChapterwiseBot, TelegramSender
from chapterwise.db import DocumentRow, Subscription
from chapterwise.worker import Worker
from tests.conftest import FakeLLM

OWNER = 4242
STRANGER = 999


class FakeBotAPI:
    """Records every Bot API call and answers just enough for python-telegram-bot."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.reject_html = False
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if "json" in (self.headers.get("Content-Type") or ""):
                    params = json.loads(raw or "{}")
                else:
                    params = {k: v[0] for k, v in parse_qs(raw).items()}
                api.calls.append((method, params))
                status, payload = api.answer(method, params)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/bot"

    def answer(self, method: str, params: dict):
        if method == "getMe":
            return 200, {
                "ok": True,
                "result": {"id": 1, "is_bot": True, "first_name": "cw", "username": "cw_bot"},
            }
        if method == "sendMessage":
            if self.reject_html and params.get("parse_mode") == "HTML":
                return 400, {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse entities",
                }
            message_id = 100 + len(self.sent())
            chat = {"id": int(params["chat_id"]), "type": "private"}
            return 200, {
                "ok": True,
                "result": {"message_id": message_id, "date": 0, "chat": chat, "text": params["text"]},
            }
        if method == "getUpdates":
            return 200, {"ok": True, "result": []}
        if method in ("editMessageText", "editMessageReplyMarkup"):
            return 200, {
                "ok": True,
                "result": {"message_id": 7, "date": 0, "chat": {"id": OWNER, "type": "private"}, "text": "x"},
            }
        return 200, {"ok": True, "result": True}

    def sent(self) -> list[dict]:
        return [p for m, p in self.calls if m == "sendMessage"]

    def calls_of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


def command(text: str, chat_id: int = OWNER, update_id: int = 1) -> dict:
    word = text.split()[0]
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Amin"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(word)}],
        },
    }


def tap(data: str, chat_id: int = OWNER, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Amin"},
            "chat_instance": "x",
            "data": data,
            "message": {
                "message_id": 7,
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "text": "earlier message",
            },
        },
    }


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    fake = FakeBotAPI()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def bot_settings(settings):
    return replace(settings, telegram_token="123:abc", telegram_chat_id=OWNER)


@pytest.fixture
def worker(sessions, bot_settings):
    w = Worker(bot_settings, sessions, llm=FakeLLM("fake-model"))
    w.schedule_scan()
    while w.run_once():
        pass
    return w


def run_updates(bot: ChapterwiseBot, api: FakeBotAPI, *updates: dict) -> None:
    async def go():
        app = bot.build(base_url=api.base_url)
        async with app:  # initialize (getMe) without starting polling or the job queue
            for data in updates:
                await app.process_update(Update.de_json(data, app.bot))

    asyncio.run(go())


def test_unconfigured_bot_tells_you_your_chat_id(sessions, bot_settings, api):
    bot = ChapterwiseBot(replace(bot_settings, telegram_chat_id=None), sessions)
    run_updates(bot, api, command("/start", chat_id=STRANGER))
    assert "Your chat ID is 999" in api.sent()[0]["text"]


def test_strangers_are_ignored(sessions, bot_settings, worker, api):
    bot = ChapterwiseBot(bot_settings, sessions)
    run_updates(
        bot, api, command("/start", chat_id=STRANGER), command("/books", chat_id=STRANGER, update_id=2)
    )
    assert api.sent() == []
    run_updates(bot, api, tap("sub:1", chat_id=STRANGER))
    assert api.sent() == []
    with sessions() as s:
        assert s.scalar(select(Subscription)) is None


def test_pick_a_book_then_next(sessions, bot_settings, worker, api):
    bot = ChapterwiseBot(bot_settings, sessions)
    with sessions() as s:
        epub = s.scalar(select(DocumentRow.id).where(DocumentRow.format == "epub"))

    run_updates(bot, api, command("/books test"))
    keyboard = json.loads(api.sent()[0]["reply_markup"])["inline_keyboard"]
    assert keyboard == [[{"text": "📖 The Test Book (3)", "callback_data": f"sub:{epub}"}]]

    run_updates(bot, api, tap(f"sub:{epub}"))
    edited = api.calls_of("editMessageText")[0]
    assert "every morning at 07:30" in edited["text"]
    with sessions() as s:
        sub = s.scalar(select(Subscription))
        assert (sub.document_id, sub.status) == (epub, "active")

    # Next before the summary exists: the bot says it's preparing...
    run_updates(bot, api, tap(f"next:{sub.id}", update_id=2))
    assert "I'll send it as soon as it's ready" in api.sent()[-1]["text"]
    # ...the worker summarizes, and the next check sends it.
    while worker.run_once():
        pass
    sender_calls = len(api.sent())

    async def check():
        app = bot.build(base_url=api.base_url)
        async with app:
            # 07:00 Rome: before the daily time, so only the pending "Next" goes out.
            now = datetime(2026, 11, 2, 7, 0, tzinfo=ZoneInfo("Europe/Rome"))
            await delivery.deliver_due(sessions, bot_settings, TelegramSender(app.bot, OWNER), now=now)

    asyncio.run(check())
    message = api.sent()[sender_calls]
    assert message["parse_mode"] == "HTML"
    assert message["text"].startswith("<b>The Test Book</b>\nChapter 1 · 1/3")
    buttons = json.loads(message["reply_markup"])["inline_keyboard"]
    assert buttons[0][0] == {"text": "Next ▶", "callback_data": f"next:{sub.id}"}


def test_reading_pause_and_stop(sessions, bot_settings, worker, api):
    bot = ChapterwiseBot(bot_settings, sessions)
    with sessions() as s:
        epub = s.scalar(select(DocumentRow.id).where(DocumentRow.format == "epub"))
        sub_id = delivery.subscribe(s, epub).id

    run_updates(bot, api, command("/reading"))
    assert "Next: section 1 of 3" in api.sent()[0]["text"]

    run_updates(bot, api, tap(f"pause:{sub_id}"))
    assert api.calls_of("answerCallbackQuery")[-1]["text"] == "Paused"
    run_updates(bot, api, tap(f"stop:{sub_id}", update_id=2))
    with sessions() as s:
        assert s.get(Subscription, sub_id).status == "stopped"

    run_updates(bot, api, tap(f"next:{sub_id}", update_id=3))
    alert = api.calls_of("answerCallbackQuery")[-1]
    assert "finished or stopped" in alert["text"] and alert["show_alert"] == "true"


def test_rejected_html_falls_back_to_plain_text(sessions, bot_settings, api):
    api.reject_html = True

    async def go():
        app = ChapterwiseBot(bot_settings, sessions).build(base_url=api.base_url)
        async with app:
            return await TelegramSender(app.bot, OWNER).send(
                "<b>Book</b> &amp; more", [[("Pause", "cb:pause:1")]]
            )

    message_id = asyncio.run(go())
    assert message_id is not None
    plain = api.sent()[-1]
    assert "parse_mode" not in plain and plain["text"] == "Book & more"
