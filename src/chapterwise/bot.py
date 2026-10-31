"""The personal Telegram bot: pick books, receive one summarized section a day, tap Next for more.

It uses long polling, so the server needs no open port or webhook. Only TELEGRAM_CHAT_ID is served;
until that is set, /start replies with the chat ID to put there.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from chapterwise import delivery
from chapterwise.config import Settings
from chapterwise.db import DocumentRow, SectionRow
from chapterwise.telegram_format import to_plain_text

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
BOOKS_PER_PAGE = 10
COMMANDS = [
    BotCommand("books", "Pick a book to read daily (add a word to search)"),
    BotCommand("reading", "Books you're reading, with progress"),
    BotCommand("next", "Get the next section now"),
    BotCommand("help", "How this works"),
]


def keyboard(buttons: list[list[delivery.Button]]) -> InlineKeyboardMarkup | None:
    rows = [
        [
            InlineKeyboardButton(label, url=target[4:])
            if target.startswith("url:")
            else InlineKeyboardButton(label, callback_data=target[3:])
            for label, target in row
        ]
        for row in buttons
        if row
    ]
    return InlineKeyboardMarkup(rows) if rows else None


class TelegramSender:
    """delivery.Sender over the Bot API, with a plain-text fallback if Telegram rejects the HTML."""

    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, html: str, buttons: list[list[delivery.Button]]) -> int | None:
        markup = keyboard(buttons)
        try:
            message = await self.bot.send_message(
                self.chat_id,
                html,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except BadRequest as exc:
            log.warning("Telegram rejected the formatted message (%s); sending plain text", exc)
            message = await self.bot.send_message(self.chat_id, to_plain_text(html), reply_markup=markup)
        return message.message_id

    async def notify(self, text: str) -> None:
        await self.bot.send_message(self.chat_id, text)


class ChapterwiseBot:
    def __init__(self, settings: Settings, sessions: sessionmaker):
        settings.require("telegram_token", "database_url")
        self.settings = settings
        self.sessions = sessions
        self.schedule = delivery.Schedule.from_settings(settings)

    # --- wiring -------------------------------------------------------------------------------------------

    def build(self, base_url: str | None = None) -> Application:
        builder = Application.builder().token(self.settings.telegram_token).post_init(self._post_init)
        if base_url:  # tests point this at a fake Bot API
            builder = builder.base_url(base_url)
        app = builder.build()

        owner = (
            filters.Chat(chat_id=self.settings.telegram_chat_id) if self.settings.telegram_chat_id else None
        )
        app.add_handler(CommandHandler(["start", "help"], self.start))
        if owner is not None:
            app.add_handler(CommandHandler("books", self.books, filters=owner))
            app.add_handler(CommandHandler("reading", self.reading, filters=owner))
            app.add_handler(CommandHandler("next", self.next_command, filters=owner))
            app.add_handler(CallbackQueryHandler(self.button))
            app.job_queue.run_repeating(
                self.check, interval=CHECK_INTERVAL_SECONDS, first=5, name="deliveries"
            )
        return app

    async def _post_init(self, app: Application) -> None:
        await app.bot.set_my_commands(COMMANDS)
        if self.settings.telegram_chat_id:
            log.info(
                "Bot ready for chat %s; daily delivery at %s %s",
                self.settings.telegram_chat_id,
                self.schedule.label(),
                self.settings.timezone,
            )
        else:
            log.warning("TELEGRAM_CHAT_ID is not set: send /start to the bot to get your chat ID")

    def is_owner(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == self.settings.telegram_chat_id

    async def db(self, fn, *args):
        """Run a blocking database function off the event loop."""

        def run():
            with self.sessions() as session:
                return fn(session, *args)

        return await asyncio.to_thread(run)

    # --- deliveries ---------------------------------------------------------------------------------------

    async def check(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        sender = TelegramSender(context.bot, self.settings.telegram_chat_id)
        try:
            await delivery.deliver_due(self.sessions, self.settings, sender)
        except Exception:
            log.exception("Delivery check failed; retrying next minute")

    # --- commands -----------------------------------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not self.settings.telegram_chat_id:
            await update.effective_message.reply_text(
                f"Hi! Your chat ID is {chat.id}.\n\nAdd TELEGRAM_CHAT_ID={chat.id} to the server's .env "
                "and restart the bot. Until then I won't answer anyone."
            )
            return
        if not self.is_owner(update):
            return  # stay silent for everyone else
        await update.effective_message.reply_text(
            "I send you one summarized section of a book every morning at "
            f"{self.schedule.label()}.\n\n"
            "/books: pick a book (/books word to search)\n"
            "/reading: what you're reading, pause or stop\n"
            "/next: the next section right now\n\n"
            "Under each section: Next ▶ for more, Pause to take a break."
        )

    async def books(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args or []).strip()

        def find(session):
            stmt = (
                select(DocumentRow.id, DocumentRow.title, DocumentRow.author, func.count(SectionRow.id))
                .join(SectionRow, SectionRow.document_id == DocumentRow.id)
                .where(DocumentRow.status == "ready")
                .group_by(DocumentRow.id)
                .order_by(func.lower(DocumentRow.title))
                .limit(BOOKS_PER_PAGE + 1)
            )
            if query:
                pattern = f"%{query}%"
                stmt = stmt.where(DocumentRow.title.ilike(pattern) | DocumentRow.author.ilike(pattern))
            return session.execute(stmt).all()

        rows = await self.db(find)
        if not rows:
            await update.effective_message.reply_text(
                "No books match." if query else "No books are ready yet. Add some to your library first."
            )
            return
        buttons = [
            [InlineKeyboardButton(f"📖 {title[:48]} ({count})", callback_data=f"sub:{doc_id}")]
            for doc_id, title, _author, count in rows[:BOOKS_PER_PAGE]
        ]
        more = "\n\nMore books exist: narrow it down with /books word." if len(rows) > BOOKS_PER_PAGE else ""
        await update.effective_message.reply_text(
            f"Tap a book to read it daily (number of sections in brackets).{more}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def reading(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        subs = await self.db(
            lambda s: [(sub, total, sub.document.title) for sub, total in delivery.open_subscriptions(s)]
        )
        if not subs:
            await update.effective_message.reply_text("You're not reading anything. Pick a book with /books.")
            return
        for sub, total, title in subs:
            state = " · paused" if sub.status == "paused" else ""
            toggle = (
                ("Resume", f"resume:{sub.id}") if sub.status == "paused" else ("Pause", f"pause:{sub.id}")
            )
            await update.effective_message.reply_text(
                f"<b>{escape(title, quote=False)}</b>\nNext: section {sub.position + 1} of {total}{state}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Next ▶", callback_data=f"next:{sub.id}"),
                            InlineKeyboardButton(toggle[0], callback_data=toggle[1]),
                            InlineKeyboardButton("Stop", callback_data=f"stop:{sub.id}"),
                        ]
                    ]
                ),
            )

    async def next_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        subs = await self.db(
            lambda s: [(sub.id, sub.document.title) for sub, _ in delivery.open_subscriptions(s)]
        )
        if not subs:
            await update.effective_message.reply_text("You're not reading anything. Pick a book with /books.")
        elif len(subs) == 1:
            await self.send_next(subs[0][0], context)
        else:
            await update.effective_message.reply_text(
                "Which book?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(title[:60], callback_data=f"next:{sub_id}")]
                        for sub_id, title in subs
                    ]
                ),
            )

    async def send_next(self, subscription_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
        await self.db(delivery.request_next, subscription_id)
        sent = await delivery.deliver_due(
            self.sessions, self.settings, TelegramSender(context.bot, self.settings.telegram_chat_id)
        )
        if sent:
            return "Here you go"
        await context.bot.send_message(
            self.settings.telegram_chat_id,
            "Summarizing the next section… I'll send it as soon as it's ready.",
        )
        return "Preparing…"

    # --- buttons ------------------------------------------------------------------------------------------

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.is_owner(update):
            await query.answer()
            return
        action, _, raw_id = (query.data or "").partition(":")
        try:
            target = int(raw_id)
        except ValueError:
            await query.answer("Unknown button")
            return

        try:
            if action == "sub":
                sub = await self.db(delivery.subscribe, target)
                title = await self.db(lambda s: s.get(DocumentRow, sub.document_id).title)
                await query.answer("Subscribed")
                await query.edit_message_text(
                    f"📚 {title}\n\nYou'll get one section every morning at {self.schedule.label()}. "
                    "Tap Next to start now.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Next ▶", callback_data=f"next:{sub.id}")]]
                    ),
                )
            elif action == "next":
                await query.answer(await self.send_next(target, context))
            elif action in ("pause", "resume", "stop"):
                status = {"pause": "paused", "resume": "active", "stop": "stopped"}[action]
                await self.db(delivery.set_status, target, status)
                await query.answer({"paused": "Paused", "active": "Resumed", "stopped": "Stopped"}[status])
                if status != "active":
                    await query.edit_message_reply_markup(reply_markup=None)
            else:
                await query.answer("Unknown button")
        except delivery.SubscriptionError as exc:
            await query.answer(str(exc), show_alert=True)


def run(settings: Settings, sessions: sessionmaker) -> None:
    ChapterwiseBot(settings, sessions).build().run_polling(allowed_updates=Update.ALL_TYPES)
