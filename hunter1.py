#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║  HUNTER — Telegram account distribution bot (single file)    ║
╚══════════════════════════════════════════════════════════════╝

README (Termux)
---------------
    pkg update && pkg upgrade
    pkg install python
    pip install -U python-telegram-bot      # the ONLY package you need
                                            # (httpx etc. are installed automatically)
    python hunter.py

Setup checklist
---------------
 1. Put your bot token in BOT_TOKEN and your numeric Telegram ID in OWNER_ID below.
 2. Add the bot as an ADMINISTRATOR of your channel (Telegram can only report
    channel membership to bots that are admins there).
 3. Keep your emoji JSON (emoji_pack_*.json) in the SAME folder as hunter.py.
    The most-used IDs are already embedded in this file, and the JSON adds all
    the rest automatically at startup.
 4. python hunter.py

Bulk import TXT format (one account per line, '#' starts a comment):
    email|password|username|plan|premium|country|expiry|verified|login_url
Only email and password are required; everything after them is optional.
    user1@mail.com|Pass123|user1|Fan|Yes|Bangladesh|2026-12-31|Yes|https://www.crunchyroll.com/login

Custom (Premium) emoji notes
----------------------------
 * Every message goes through ONE renderer (EmojiRegistry.render). It wraps every
   emoji it knows in <tg-emoji emoji-id="..."> automatically, so every line of
   every screen (user + admin) gets the custom emoji. Code blocks are skipped.
 * Buttons use `icon_custom_emoji_id` and coloured `style` (success / primary /
   danger) from the official Bot API.
 * Telegram only allows bots to use custom emoji when the bot OWNER has
   Telegram Premium (private chats) or the bot bought extra usernames on
   Fragment. If Telegram refuses, Hunter automatically falls back to normal
   Unicode emoji so users never see a broken message.
"""

from __future__ import annotations

import asyncio
import glob
import html
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from telegram import (
        BotCommand,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        LinkPreviewOptions,
        ReplyKeyboardMarkup,
        Update,
    )
    from telegram.constants import ChatMemberStatus, ChatType, ParseMode
    from telegram.error import (
        BadRequest,
        Forbidden,
        InvalidToken,
        NetworkError,
        RetryAfter,
        TelegramError,
        TimedOut,
    )
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency.\nRun:  pip install -U python-telegram-bot")

# ════════════════════════════════════════════════════════════════
#  1. CONFIGURATION  (edit this block only)
# ════════════════════════════════════════════════════════════════

# Get the token from @BotFather. NEVER share it. Put it between the quotes.
BOT_TOKEN = "8803275187:AAGx8NeoPJD3cjDzSvlQNXNvVl8DG6Fzfeo"

# Your numeric Telegram user ID (get it from @userinfobot). Only this ID can
# open the admin panel and use every owner-only function.
OWNER_ID = 8200980090

# Mandatory channel (the bot must be an ADMIN of this channel)
CHANNEL_ID = -1002740009398
CHANNEL_LINK = "https://t.me/+2Fxg6o4jEKAxOGQ1"

# Optional tuning
DB_FILE = "hunter.db"                      # SQLite file (created automatically)
LOG_FILE = "hunter.log"                    # rotating log file (no secrets inside)
EMOJI_FILE = ""                            # optional explicit path to the emoji JSON
DEFAULT_SERVICE_NAME = "CRUNCHYROLL PREMIUM"
DEFAULT_BOT_CREDIT = "Hunter"
DEFAULT_LOGIN_URL = "https://www.crunchyroll.com/login"
HUNT_COOLDOWN_SECONDS = 15                 # default per-user cooldown (owner can change)
MEMBERSHIP_CACHE_TTL = 60                  # seconds a *positive* membership result is reused
ANIMATION_DELAY = 0.7                      # seconds between animation frames
MAX_TXT_BYTES = 5 * 1024 * 1024            # bulk import size limit
PAGE_SIZE = 8                              # rows per page in admin lists

# ════════════════════════════════════════════════════════════════
#  2. LOGGING  (token / password safe)
# ════════════════════════════════════════════════════════════════

_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


class RedactFilter(logging.Filter):
    """Scrubs the bot token from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = _TOKEN_RE.sub("<token>", msg)
        if BOT_TOKEN and BOT_TOKEN != "PUT_BOT_TOKEN_HERE" and BOT_TOKEN in red:
            red = red.replace(BOT_TOKEN, "<token>")
        if red != msg:
            record.msg, record.args = red, ()
        return True


def setup_logging() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        root.addHandler(h)
    # httpx logs full request URLs (which contain the token) at INFO level.
    for noisy in ("httpx", "httpcore", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("hunter")


log = setup_logging()

# ════════════════════════════════════════════════════════════════
#  3. PREMIUM / CUSTOM EMOJI SYSTEM
# ════════════════════════════════════════════════════════════════
# EMOJI_TABLE  : unicode emoji -> custom emoji ID (taken from your uploaded JSON)
# EMOJI_ALIASES: emoji you use in text that has no exact twin in your packs ->
#                the closest emoji that does. Add/change freely.
# At startup the JSON file is loaded too, so every emoji in your packs is usable
# by simply typing the normal emoji in any message template.

EMOJI_TABLE: dict[str, str] = {
    "🏡": "4958485609464202497",
    "🔖": "6309928100289322286",
    "📚": "5222444124698853913",
    "📣": "6309618596356039410",
    "👤": "5902335789798265487",
    "🛍": "6309716736358751850",
    "🎈": "4956234552679859009",
    "🚩": "6309813746785066712",
    "😞": "6309687448976760879",
    "⬆": "5415655814079723871",
    "⬇": "5406745015365943482",
    "🔐": "6309599324837781578",
    "✉": "6312231491250165922",
    "🌎": "6309899938188762421",
    "🖊": "5395444784611480792",
    "🔸": "6312142645556680754",
    "💠": "6309958250959739643",
    "🕰": "5440621591387980068",
    "🔄": "5375338737028841420",
    "🧐": "6310087310432018431",
    "👍": "6309697344581410091",
    "🛡": "5251203410396458957",
    "🆗": "4956649845952611245",
    "⏩": "6309994015152413596",
    "⏰": "5902050947567194830",
    "📌": "6309960153630251694",
    "🔒": "6310091871687286225",
    "✅": "6309618368722770953",
    "🎉": "6309961712703379735",
    "🚀": "6309870788245724520",
    "❌": "5210952531676504517",
    "⚠": "6309743751703042732",
    "🔧": "5341715473882955310",
    "🚫": "6312156428106737991",
    "⛔": "5260293700088511294",
    "🔎": "5893382531037794941",
    "ℹ": "5334544901428229844",
    "⚙": "5893161718179173515",
    "💬": "6310075555106528104",
    "🎁": "6312282231993801242",
    "⏳": "5386367538735104399",
    "🔍": "5231012545799666522",
    "⚡": "6309883501348919953",
    "✨": "6312315895947467982",
    "👑": "6310083389126876253",
    "??": "6310032545304026289",
    "🌐": "5447410659077661506",
    "📅": "5413879192267805083",
    "🤖": "6309920764485180319",
    "🔗": "6309893061946121745",
    "🟢": "5416081784641168838",
    "🔵": "6310043042204097096",
    "➕": "5397916757333654639",
    "📊": "6309843875980648126",
    "🗑": "6309887495668505491",
    "🔼": "6309994659397507681",
    "🔽": "5447183459602669338",
    "🆕": "5382357040008021292",
    "🔥": "6309811869884358547",
    "📈": "5244837092042750681",
    "🔴": "5411225014148014586",
}

EMOJI_ALIASES: dict[str, str] = {
    "🏠": "🏡", "📋": "🔖", "📜": "📚", "📢": "📣", "👥": "👤", "📦": "🛍",
    "🍿": "🎈", "🎯": "🚩", "😔": "😞", "📤": "⬆", "📥": "⬇", "🔑": "🔐",
    "📧": "✉", "🌍": "🌎", "📝": "🖊", "🟡": "🔸", "🟣": "💠", "🕒": "🕰",
    "🔁": "🔄", "🤔": "🧐", "🙌": "👍", "🔰": "🛡", "👌": "🆗", "⏭": "⏩",
    "⏱": "⏰", "🔓": "🔐", "🔃": "🔄", "⬅": "⏩",
    "✏": "🖊", "📄": "📚", "🧾": "📚",
}


def _norm(ch: str) -> str:
    return ch.replace("\ufe0f", "")


class Runtime:
    """Runtime switches (owner can toggle them from Settings)."""
    rich = True        # use custom emoji (tg-emoji + button icons)
    animation = True


class EmojiRegistry:
    _PROTECTED = re.compile(r"(<code>.*?</code>|<pre>.*?</pre>)", re.S)

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.pattern: Optional[re.Pattern[str]] = None

    def load_defaults(self) -> None:
        for k, v in EMOJI_TABLE.items():
            self.ids[_norm(k)] = str(v)

    def load_json(self, path: str) -> int:
        """Load extra emoji from the uploaded pack JSON. Returns how many were added."""
        added = 0
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for pack in (data.get("packs") or {}).values():
            for item in pack.get("emojis", []):
                ch, eid = _norm(str(item.get("emoji", ""))), str(item.get("id", ""))
                if ch and eid.isdigit() and ch not in self.ids:
                    self.ids[ch] = eid
                    added += 1
        return added

    def finalize(self) -> None:
        for alias, target in EMOJI_ALIASES.items():
            a, t = _norm(alias), _norm(target)
            if a not in self.ids and t in self.ids:
                self.ids[a] = self.ids[t]
        keys = [k for k in self.ids if k and ord(k[0]) >= 0x2000]   # skip digits, ©, ®
        keys.sort(key=len, reverse=True)
        self.pattern = re.compile("(" + "|".join(re.escape(k) for k in keys) + ")\ufe0f?") if keys else None

    def id_of(self, ch: Optional[str]) -> Optional[str]:
        return self.ids.get(_norm(ch)) if ch else None

    def has_emoji(self, text: str) -> bool:
        return bool(self.pattern and self.pattern.search(text))

    def _sub(self, m: re.Match[str]) -> str:
        eid = self.ids.get(_norm(m.group(1)))
        return f'<tg-emoji emoji-id="{eid}">{m.group(0)}</tg-emoji>' if eid else m.group(0)

    def render(self, text: str, rich: bool = True) -> str:
        """Wrap every known emoji in <tg-emoji>. Skips <code>/<pre> blocks."""
        if not (rich and Runtime.rich and self.pattern):
            return text
        parts = self._PROTECTED.split(text)
        for i in range(0, len(parts), 2):          # even indexes = normal text
            parts[i] = self.pattern.sub(self._sub, parts[i])
        return "".join(parts)

    def ensure_line_emoji(self, text: str, default: str = "📌") -> str:
        """Owner-written text: give every line an emoji so the UI stays consistent."""
        out = []
        for line in text.splitlines():
            out.append(line if (not line.strip() or self.has_emoji(line)) else f"{default} {line}")
        return "\n".join(out)


EMOJI = EmojiRegistry()


def init_emoji() -> None:
    EMOJI.load_defaults()
    candidates: list[str] = []
    if EMOJI_FILE:
        candidates.append(EMOJI_FILE)
    base = Path(__file__).resolve().parent
    for folder in (base, Path.cwd()):
        candidates += sorted(glob.glob(str(folder / "emoji*.json")))
    seen: set[str] = set()
    for path in candidates:
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        try:
            n = EMOJI.load_json(path)
            log.info("Emoji pack loaded: %s (+%d extra emoji)", os.path.basename(path), n)
        except (OSError, ValueError) as exc:
            log.warning("Could not read emoji file %s: %s", path, exc)
    EMOJI.finalize()
    log.info("Custom emoji ready: %d mapped emoji", len(EMOJI.ids))


# ════════════════════════════════════════════════════════════════
#  4. UI HELPERS  (text, buttons, safe send/edit)
# ════════════════════════════════════════════════════════════════

Ctx = ContextTypes.DEFAULT_TYPE
KbFactory = Callable[[bool], Any]          # rich(bool) -> reply_markup
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:2]}•••@{domain}" if domain else f"{(email or '')[:2]}•••"


def ib(label: str, *, cb: Optional[str] = None, url: Optional[str] = None,
       style: Optional[str] = None, icon: Optional[str] = None, rich: bool = True) -> InlineKeyboardButton:
    """Inline button with coloured style + custom emoji icon (official Bot API fields)."""
    extra: dict[str, Any] = {}
    text = label
    icon_id = EMOJI.id_of(icon)
    if rich and style:
        extra["style"] = style
    if rich and Runtime.rich and icon_id:
        extra["icon_custom_emoji_id"] = icon_id
    elif icon:
        text = f"{icon} {label}"
    return InlineKeyboardButton(text, callback_data=cb, url=url, api_kwargs=extra or None)


def rb(label: str, icon: str, style: Optional[str] = None, rich: bool = True) -> KeyboardButton:
    """Reply-keyboard button (Bot API 9.4 supports style + icon here too)."""
    extra: dict[str, Any] = {}
    text = label
    icon_id = EMOJI.id_of(icon)
    if rich and style:
        extra["style"] = style
    if rich and Runtime.rich and icon_id:
        extra["icon_custom_emoji_id"] = icon_id
    else:
        text = f"{icon} {label}"
    return KeyboardButton(text, api_kwargs=extra or None)


async def _tg(factory: Callable[[], Any], retries: int = 2) -> Any:
    """Run a Telegram call, retrying flood-waits and transient network errors."""
    for attempt in range(retries + 1):
        try:
            return await factory()
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
        except (TimedOut, NetworkError):
            if attempt == retries:
                raise
            await asyncio.sleep(1 + attempt)
    return None


class Ui:
    """The only place that talks to Telegram for UI messages."""

    @staticmethod
    async def send(bot: Any, chat_id: int, text: str, kb: Optional[KbFactory] = None) -> Any:
        for rich in (True, False):
            try:
                return await _tg(lambda: bot.send_message(
                    chat_id=chat_id, text=EMOJI.render(text, rich), parse_mode=ParseMode.HTML,
                    reply_markup=kb(rich) if kb else None, link_preview_options=NO_PREVIEW))
            except BadRequest as exc:
                log.warning("send BadRequest (rich=%s): %s", rich, exc)
            except Forbidden:
                log.info("User %s blocked the bot or chat is closed", chat_id)
                return None
            except TelegramError as exc:
                log.error("send failed: %s", exc)
                return None
        return None

    @staticmethod
    async def edit(bot: Any, chat_id: int, message_id: int, text: str,
                   kb: Optional[KbFactory] = None) -> Optional[int]:
        """Edit a message. If it is gone/expired, send a fresh one. Returns the message id."""
        for rich in (True, False):
            try:
                await _tg(lambda: bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=EMOJI.render(text, rich),
                    parse_mode=ParseMode.HTML, reply_markup=kb(rich) if kb else None,
                    link_preview_options=NO_PREVIEW))
                return message_id
            except BadRequest as exc:
                low = str(exc).lower()
                if "message is not modified" in low:
                    return message_id
                if "not found" in low or "can't be edited" in low or "message_id_invalid" in low:
                    break
                log.warning("edit BadRequest (rich=%s): %s", rich, exc)
            except Forbidden:
                return None
            except TelegramError as exc:
                log.error("edit failed: %s", exc)
                return None
        msg = await Ui.send(bot, chat_id, text, kb)
        return msg.message_id if msg else None

    @staticmethod
    async def show(bot: Any, chat_id: int, message_id: Optional[int], text: str,
                   kb: Optional[KbFactory] = None) -> Optional[int]:
        if message_id:
            return await Ui.edit(bot, chat_id, message_id, text, kb)
        msg = await Ui.send(bot, chat_id, text, kb)
        return msg.message_id if msg else None


async def safe_answer(q: Any, text: Optional[str] = None, alert: bool = False) -> None:
    try:
        await q.answer(text=text, show_alert=alert)
    except TelegramError as exc:            # "query is too old" etc.
        log.debug("callback answer skipped: %s", exc)


# ════════════════════════════════════════════════════════════════
#  5. MESSAGE TEMPLATES  (every line carries an emoji)
# ════════════════════════════════════════════════════════════════

class Msg:
    @staticmethod
    def join() -> str:
        return ("🔒 <b>CHANNEL VERIFICATION</b>\n\n"
                "🛡 You must join our official channel before using <b>Hunter</b>.\n\n"
                "📢 Join the channel first, then press the verification button below.")

    @staticmethod
    def verified() -> str:
        return ("✅ <b>CHANNEL VERIFIED</b>\n\n"
                "🎉 Welcome to <b>Hunter</b>!\n\n"
                "🚀 Your access is unlocked.")

    @staticmethod
    def verify_failed() -> str:
        return ("❌ <b>VERIFICATION FAILED</b>\n\n"
                "⚠️ You have not joined the required channel yet.\n\n"
                "🔄 Please join the channel and try again.")

    @staticmethod
    def verify_error() -> str:
        return ("⚠️ <b>VERIFICATION UNAVAILABLE</b>\n\n"
                "🔧 We couldn't check your membership right now.\n\n"
                "🔄 Please try again in a few moments.")

    @staticmethod
    def banned() -> str:
        return "🚫 <b>ACCESS RESTRICTED</b>\n\n⛔ Your access to this bot has been disabled."

    @staticmethod
    def menu(owner: bool) -> str:
        text = ("🏠 <b>MAIN MENU</b>\n\n"
                "🔎 <b>Hunt Account</b> — find an available account\n"
                "📋 <b>My Account</b> — your profile &amp; stats\n"
                "📜 <b>History</b> — accounts you received\n"
                "📢 <b>Updates</b> — latest news\n"
                "ℹ️ <b>Help</b> — how Hunter works")
        if owner:
            text += "\n⚙️ <b>Admin Panel</b> — manage the bot"
        return text

    @staticmethod
    def hint() -> str:
        return "💬 Please choose an option from the menu below.\n\n🏠 Use the buttons to navigate."

    @staticmethod
    def help() -> str:
        return ("ℹ️ <b>HELP</b>\n\n"
                "🔎 Press <b>Hunt Account</b> and Hunter searches the stock for you.\n"
                "🎁 Every account is delivered to one user only — never shared twice.\n"
                "📜 Open <b>History</b> to see accounts you already received.\n"
                "📋 <b>My Account</b> shows your profile and usage.\n"
                "⏳ A short cooldown protects the stock from spam.\n"
                "💬 Need something else? Contact the bot owner.")

    @staticmethod
    def hunt_frame(idx: int) -> str:
        titles = ["🔎 Hunting for an account...", "🔍 Searching available stock...",
                  "⚡ Checking account inventory...", "🎯 Finding an available account...",
                  "✨ Account found!"]
        bar = "▰" * (idx + 1) + "▱" * (4 - idx)
        return f"<b>{titles[idx]}</b>\n\n🚀 {bar} {(idx + 1) * 20}%"

    @staticmethod
    def no_stock() -> str:
        return "😔 <b>No account is available right now.</b>\n\n⏳ Please try again later."

    @staticmethod
    def cooldown(seconds: int) -> str:
        return f"⏳ <b>PLEASE WAIT</b>\n\n🔄 You can hunt again in <b>{seconds}s</b>."

    @staticmethod
    def error() -> str:
        return "⚠️ <b>Something went wrong.</b>\n\n🔄 Please try again in a moment."

    @staticmethod
    def delivery(acc: dict[str, Any], service: str, credit: str) -> str:
        def g(key: str) -> str:
            return esc(acc.get(key) or "N/A")
        return (
            f"👑 <b>{esc(service)} ACCOUNT DELIVERED</b> ⚡\n\n"
            "👤 <b>ACCOUNT CREDENTIALS:</b>\n\n"
            f"📧 Email: <code>{g('email')}</code>\n"
            f"🔐 Password: <code>{g('password')}</code>\n"
            f"👤 Username: <code>{g('username')}</code>\n\n"
            "💎 <b>SUBSCRIPTION INFO:</b>\n\n"
            f"💎 Plan: {g('plan')}\n"
            f"👑 Premium: {g('premium')}\n"
            f"🌐 Country: {g('country')}\n"
            f"📅 Expiry: {g('expiry')}\n"
            f"✅ Email Verified: {g('email_verified')}\n\n"
            f"🍿 Login: {g('login_url')}\n\n"
            f"🤖 Bot by: {esc(credit)}"
        )

    @staticmethod
    def my_account(u: dict[str, Any], received: int, is_owner: bool) -> str:
        uname = f"@{esc(u['username'])}" if u.get("username") else "N/A"
        role = "Owner" if is_owner else "Member"
        return (f"📋 <b>MY ACCOUNT</b>\n\n"
                f"👤 Name: {esc(u.get('first_name') or 'N/A')}\n"
                f"📧 Username: {uname}\n"
                f"🔐 User ID: <code>{u['user_id']}</code>\n"
                f"👑 Role: {role}\n"
                f"✅ Channel: {'Verified' if u.get('is_verified') or is_owner else 'Not verified'}\n"
                f"📅 Joined: {esc(u.get('first_seen'))} UTC\n"
                f"🎁 Accounts received: <b>{received}</b>")

    @staticmethod
    def history(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "📜 <b>HISTORY</b>\n\n😔 You haven't received any account yet.\n\n🔎 Press Hunt Account to get started."
        lines = ["📜 <b>HISTORY</b>", "", "🎁 Your latest accounts (tap a number to view):", ""]
        for r in rows:
            lines.append(f"📌 <b>#{r['assign_id']}</b> · {esc(mask_email(r['email']))} · "
                         f"{esc(r.get('plan') or 'N/A')} · {esc(r['assigned_at'][:10])}")
        return "\n".join(lines)

    @staticmethod
    def admin_home(st: dict[str, int]) -> str:
        return (f"⚙️ <b>ADMIN PANEL</b>\n\n"
                f"👑 Welcome back, owner.\n\n"
                f"📦 Available stock: <b>{st['available']}</b>\n"
                f"📤 Distributed: <b>{st['distributed']}</b>\n"
                f"👥 Users: <b>{st['users']}</b>\n\n"
                f"🚀 Choose an action below.")


# ════════════════════════════════════════════════════════════════
#  6. DATABASE  (SQLite, parameterised queries, safe transactions)
# ════════════════════════════════════════════════════════════════

ACCOUNT_FIELDS: list[tuple[str, str, str, bool]] = [
    # key, label, icon, required
    ("email", "Email", "📧", True),
    ("password", "Password", "🔐", True),
    ("username", "Username", "👤", False),
    ("plan", "Plan", "💎", False),
    ("premium", "Premium", "👑", False),
    ("country", "Country", "🌐", False),
    ("expiry", "Expiry", "📅", False),
    ("email_verified", "Email Verified", "✅", False),
    ("login_url", "Login URL", "🔗", False),
]
FIELD_KEYS = [f[0] for f in ACCOUNT_FIELDS]

SETTING_DEFAULTS = {
    "service_name": DEFAULT_SERVICE_NAME,
    "bot_credit": DEFAULT_BOT_CREDIT,
    "updates_text": ("📢 Stay tuned — new stock is added regularly.\n"
                     "🚀 Follow our official channel for announcements.\n"
                     "🎁 Thank you for using Hunter!"),
    "cooldown": str(HUNT_COOLDOWN_SECONDS),
    "animation": "1",
    "rich_emoji": "1",
    "join_required": "1",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        try:
            os.chmod(path, 0o600)           # credentials live here: owner-only file
        except OSError:
            pass

    # ---- infrastructure -------------------------------------------------
    @staticmethod
    async def run(fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    @contextmanager
    def _tx(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
                cur.execute("COMMIT")
            except BaseException:
                try:
                    cur.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                cur.close()

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def _val(self, sql: str, params: tuple = ()) -> Any:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return r[0] if r else None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _create_schema(self) -> None:
        with self._tx() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                is_verified INTEGER NOT NULL DEFAULT 0, is_banned INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, verified_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS accounts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL, password TEXT NOT NULL, username TEXT, plan TEXT,
                premium TEXT, country TEXT, expiry TEXT, email_verified TEXT, login_url TEXT,
                status TEXT NOT NULL DEFAULT 'available', added_at TEXT NOT NULL, distributed_at TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status, id)")
            c.execute("""CREATE TABLE IF NOT EXISTS assignments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id),
                user_id INTEGER NOT NULL, assigned_at TEXT NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_assign_user ON assignments(user_id, id)")
            c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for k, v in SETTING_DEFAULTS.items():
                c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))

    # ---- settings ---------------------------------------------------------
    def get_setting(self, key: str) -> str:
        v = self._val("SELECT value FROM settings WHERE key=?", (key,))
        return v if v is not None else SETTING_DEFAULTS.get(key, "")

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---- users ------------------------------------------------------------
    def touch_user(self, uid: int, username: Optional[str], first_name: Optional[str]) -> None:
        now = utcnow()
        with self._tx() as c:
            c.execute("""INSERT INTO users(user_id, username, first_name, first_seen, last_seen)
                         VALUES(?, ?, ?, ?, ?)
                         ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                         first_name=excluded.first_name, last_seen=excluded.last_seen""",
                      (uid, username, first_name, now, now))

    def set_verified(self, uid: int, flag: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE users SET is_verified=?, verified_at=CASE WHEN ? THEN ? ELSE verified_at END "
                      "WHERE user_id=?", (1 if flag else 0, 1 if flag else 0, utcnow(), uid))

    def is_banned(self, uid: int) -> bool:
        return bool(self._val("SELECT is_banned FROM users WHERE user_id=?", (uid,)))

    def set_banned(self, uid: int, flag: bool) -> bool:
        with self._tx() as c:
            c.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if flag else 0, uid))
            return c.rowcount == 1

    def get_user(self, uid: int) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM users WHERE user_id=?", (uid,))

    def list_users(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        total = int(self._val("SELECT COUNT(*) FROM users") or 0)
        rows = self._all("SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?", (limit, offset))
        return rows, total

    def broadcast_targets(self) -> list[int]:
        return [r["user_id"] for r in self._all(
            "SELECT user_id FROM users WHERE is_verified=1 AND is_banned=0")]

    # ---- accounts ---------------------------------------------------------
    @staticmethod
    def _row_values(d: dict[str, Any]) -> tuple:
        return (d["email"].strip().lower(), d["email"], d["password"], d.get("username"), d.get("plan"),
                d.get("premium"), d.get("country"), d.get("expiry"), d.get("email_verified"),
                d.get("login_url"), utcnow())

    _INSERT = ("INSERT INTO accounts(dedupe_key, email, password, username, plan, premium, country, "
               "expiry, email_verified, login_url, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)")

    def add_account(self, d: dict[str, Any]) -> bool:
        """True if added, False if it is a duplicate."""
        try:
            with self._tx() as c:
                c.execute(self._INSERT, self._row_values(d))
            return True
        except sqlite3.IntegrityError:
            return False

    def bulk_insert(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        added = dups = 0
        with self._tx() as c:
            for d in rows:
                try:
                    c.execute("SAVEPOINT sp")
                    c.execute(self._INSERT, self._row_values(d))
                    c.execute("RELEASE sp")
                    added += 1
                except sqlite3.IntegrityError:
                    c.execute("ROLLBACK TO sp")
                    c.execute("RELEASE sp")
                    dups += 1
        return added, dups

    def assign_account(self, uid: int) -> Optional[dict[str, Any]]:
        """Atomically reserve the oldest available account for `uid`."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM accounts WHERE status='available' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            now = utcnow()
            c.execute("UPDATE accounts SET status='distributed', distributed_at=? "
                      "WHERE id=? AND status='available'", (now, row["id"]))
            if c.rowcount != 1:
                return None
            c.execute("INSERT INTO assignments(account_id, user_id, assigned_at) VALUES(?,?,?)",
                      (row["id"], uid, now))
            data = dict(row)
            data["assign_id"] = c.lastrowid
            data["assigned_at"] = now
            return data

    def get_account(self, aid: int) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM accounts WHERE id=?", (aid,))

    def remove_account(self, aid: int) -> bool:
        """Only accounts that were never distributed can be removed (keeps history intact)."""
        with self._tx() as c:
            c.execute("DELETE FROM accounts WHERE id=? AND status='available'", (aid,))
            return c.rowcount == 1

    def list_available(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        total = int(self._val("SELECT COUNT(*) FROM accounts WHERE status='available'") or 0)
        rows = self._all("SELECT id, email, plan, country, added_at FROM accounts "
                         "WHERE status='available' ORDER BY id LIMIT ? OFFSET ?", (limit, offset))
        return rows, total

    def list_distributed(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        total = int(self._val("SELECT COUNT(*) FROM assignments") or 0)
        rows = self._all("""SELECT a.id AS assign_id, a.assigned_at, a.user_id, c.id AS account_id, c.email,
                            c.plan, u.username FROM assignments a JOIN accounts c ON c.id=a.account_id
                            LEFT JOIN users u ON u.user_id=a.user_id
                            ORDER BY a.id DESC LIMIT ? OFFSET ?""", (limit, offset))
        return rows, total

    # ---- history ----------------------------------------------------------
    def user_history(self, uid: int, limit: int = 8) -> list[dict[str, Any]]:
        return self._all("""SELECT a.id AS assign_id, a.assigned_at, c.email, c.plan FROM assignments a
                            JOIN accounts c ON c.id=a.account_id WHERE a.user_id=?
                            ORDER BY a.id DESC LIMIT ?""", (uid, limit))

    def get_assignment(self, assign_id: int, uid: int) -> Optional[dict[str, Any]]:
        return self._one("""SELECT c.*, a.id AS assign_id, a.assigned_at FROM assignments a
                            JOIN accounts c ON c.id=a.account_id WHERE a.id=? AND a.user_id=?""",
                         (assign_id, uid))

    def user_received(self, uid: int) -> int:
        return int(self._val("SELECT COUNT(*) FROM assignments WHERE user_id=?", (uid,)) or 0)

    # ---- statistics ---------------------------------------------------------
    def quick_stats(self) -> dict[str, int]:
        return {
            "available": int(self._val("SELECT COUNT(*) FROM accounts WHERE status='available'") or 0),
            "distributed": int(self._val("SELECT COUNT(*) FROM accounts WHERE status='distributed'") or 0),
            "users": int(self._val("SELECT COUNT(*) FROM users") or 0),
        }

    def full_stats(self) -> dict[str, int]:
        d = self.quick_stats()
        d["users_verified"] = int(self._val("SELECT COUNT(*) FROM users WHERE is_verified=1") or 0)
        d["users_banned"] = int(self._val("SELECT COUNT(*) FROM users WHERE is_banned=1") or 0)
        d["users_24h"] = int(self._val("SELECT COUNT(*) FROM users WHERE first_seen>=?", (_ago(1),)) or 0)
        d["dist_24h"] = int(self._val("SELECT COUNT(*) FROM assignments WHERE assigned_at>=?", (_ago(1),)) or 0)
        d["dist_7d"] = int(self._val("SELECT COUNT(*) FROM assignments WHERE assigned_at>=?", (_ago(7),)) or 0)
        d["total"] = d["available"] + d["distributed"]
        return d


# ════════════════════════════════════════════════════════════════
#  7. VALIDATION & PARSING
# ════════════════════════════════════════════════════════════════

EMAIL_RE = re.compile(r"^[^@\s|]{1,64}@[^@\s|]+\.[^@\s|]{2,}$")
URL_RE = re.compile(r"^https?://\S+$", re.I)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean(value: str) -> str:
    return _CTRL_RE.sub("", value or "").strip()


def norm_flag(value: str) -> str:
    s = value.strip().lower()
    if s in {"yes", "y", "true", "1", "verified", "✅"}:
        return "Yes"
    if s in {"no", "n", "false", "0", "unverified", "❌"}:
        return "No"
    return value.strip()


def validate_account(raw: dict[str, str]) -> tuple[Optional[dict[str, Any]], str]:
    """Returns (normalised_account, "") or (None, reason)."""
    d = {k: clean(raw.get(k, "")) for k in FIELD_KEYS}
    if not d["email"] or len(d["email"]) > 254 or not EMAIL_RE.match(d["email"]):
        return None, "invalid email"
    if not d["password"] or len(d["password"]) > 200:
        return None, "invalid password"
    for k in FIELD_KEYS[2:8]:
        if len(d[k]) > 100:
            return None, f"{k} too long"
    if d["login_url"] and (len(d["login_url"]) > 300 or not URL_RE.match(d["login_url"])):
        return None, "invalid login url"
    d["login_url"] = d["login_url"] or DEFAULT_LOGIN_URL
    if d["email_verified"]:
        d["email_verified"] = norm_flag(d["email_verified"])
    for k in FIELD_KEYS[2:8]:
        d[k] = d[k] or ""
    return d, ""


def parse_line(line: str) -> tuple[Optional[dict[str, Any]], str]:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2:
        return None, "missing password"
    if len(parts) > len(FIELD_KEYS):
        return None, "too many fields"
    return validate_account(dict(zip(FIELD_KEYS, parts)))


def parse_txt(data: bytes) -> tuple[list[dict[str, Any]], list[int], int]:
    """Returns (valid_rows, invalid_line_numbers, total_processed). Never raises on bad lines."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    valid: list[dict[str, Any]] = []
    invalid: list[int] = []
    total = 0
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        total += 1
        try:
            row, _reason = parse_line(line[:1000])
        except Exception:                   # a malformed line must never crash the import
            row = None
        if row:
            valid.append(row)
        else:
            invalid.append(no)
    return valid, invalid, total


# ════════════════════════════════════════════════════════════════
#  8. KEYBOARDS
# ════════════════════════════════════════════════════════════════

def kb_main(owner: bool) -> KbFactory:
    def make(rich: bool = True) -> ReplyKeyboardMarkup:
        rows = [
            [rb("Hunt Account", "🔎", "success", rich), rb("My Account", "📋", "primary", rich)],
            [rb("History", "📜", "primary", rich), rb("Updates", "📢", "primary", rich)],
            [rb("Help", "ℹ️", "primary", rich)],
        ]
        if owner:
            rows[2].append(rb("Admin Panel", "⚙️", "danger", rich))
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                                   input_field_placeholder="Choose an option…")
    return make


def kb_join() -> KbFactory:
    def make(rich: bool = True) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [ib("Join Channel", url=CHANNEL_LINK, style="success", icon="🟢", rich=rich)],
            [ib("Verify Membership", cb="verify", style="primary", icon="🔵", rich=rich)],
        ])
    return make


def kb_rows(*rows: list[tuple]) -> KbFactory:
    """Build an inline keyboard from tuples: (label, callback, style, icon)."""
    def make(rich: bool = True) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [ib(lbl, cb=cb, style=st, icon=ic, rich=rich) for (lbl, cb, st, ic) in row] for row in rows
        ])
    return make


KB_HUNT_AGAIN = kb_rows([("Hunt Again", "hunt", "success", "🔎"), ("History", "hist:list", "primary", "📜")])
KB_RETRY = kb_rows([("Try Again", "hunt", "primary", "🔄")])
KB_ACCOUNT = kb_rows([("Hunt Account", "hunt", "success", "🔎"), ("History", "hist:list", "primary", "📜")])
KB_ADMIN = kb_rows(
    [("Add Account", "adm:add", "success", "➕"), ("Account Stock", "adm:stock:0", "primary", "📦")],
    [("Distributed Accounts", "adm:dist:0", "primary", "📤"), ("Users", "adm:users", "primary", "👥")],
    [("Statistics", "adm:stats", "primary", "📊"), ("Remove Account", "adm:remove", "danger", "🗑️")],
    [("Broadcast", "adm:bc", "primary", "📢"), ("Settings", "adm:settings", "primary", "⚙️")],
    [("Bulk Import", "adm:bulk", "success", "📦")],
    [("User Panel", "adm:user_panel", "primary", "🏠")],
)
KB_BACK_ADMIN = kb_rows([("Admin Panel", "adm:home", "primary", "⚙️")])
KB_CANCEL = kb_rows([("Cancel", "st:cancel", "danger", "❌")])
KB_SKIP = kb_rows([("Skip", "st:skip", "primary", "⏩"), ("Cancel", "st:cancel", "danger", "❌")])
KB_USERS = kb_rows(
    [("Ban User", "adm:ban", "danger", "🚫"), ("Unban User", "adm:unban", "success", "✅")],
    [("Admin Panel", "adm:home", "primary", "⚙️")],
)
KB_BULK_DONE = kb_rows(
    [("Upload Another", "adm:bulk", "success", "📦"), ("Admin Panel", "bulk:done", "primary", "⚙️")],
)
KB_BULK_WAIT = kb_rows([("Done", "bulk:done", "primary", "✅"), ("Cancel", "st:cancel", "danger", "❌")])
KB_ADD_DONE = kb_rows([("Add Another", "adm:add", "success", "➕"), ("Admin Panel", "adm:home", "primary", "⚙️")])
KB_BC_CONFIRM = kb_rows([("Send Now", "bc:yes", "success", "🚀"), ("Cancel", "bc:no", "danger", "❌")])


def kb_pager(kind: str, page: int, pages: int) -> KbFactory:
    row: list[tuple] = []
    if page > 0:
        row.append(("Prev", f"adm:{kind}:{page - 1}", "primary", "🔼"))
    if page + 1 < pages:
        row.append(("Next", f"adm:{kind}:{page + 1}", "primary", "🔽"))
    rows = ([row] if row else []) + [[("Admin Panel", "adm:home", "primary", "⚙️")]]
    return kb_rows(*rows)


def kb_history(rows: list[dict[str, Any]]) -> KbFactory:
    def make(rich: bool = True) -> InlineKeyboardMarkup:
        buttons = [ib(f"#{r['assign_id']}", cb=f"hist:view:{r['assign_id']}", style="primary",
                      icon="🎁", rich=rich) for r in rows]
        grid = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        grid.append([ib("Hunt Account", cb="hunt", style="success", icon="🔎", rich=rich)])
        return InlineKeyboardMarkup(grid)
    return make


def kb_remove_confirm(aid: int) -> KbFactory:
    return kb_rows([("Yes, Remove", f"rm:yes:{aid}", "danger", "🗑️"), ("Cancel", "st:cancel", "primary", "❌")])


def kb_settings() -> KbFactory:
    anim_state = "ON" if Runtime.animation else "OFF"
    rich_state = "ON" if Runtime.rich else "OFF"
    return kb_rows(
        [(f"Animation: {anim_state}", "set:anim", "success" if Runtime.animation else "danger", "🚀"),
         (f"Premium Emoji: {rich_state}", "set:rich", "success" if Runtime.rich else "danger", "✨")],
        [("Channel Join", "set:join", "primary", "🔒"), ("Cooldown", "set:edit:cooldown", "primary", "⏳")],
        [("Service Name", "set:edit:service_name", "primary", "👑"), ("Bot Credit", "set:edit:bot_credit", "primary", "🤖")],
        [("Updates Text", "set:edit:updates_text", "primary", "📢")],
        [("Restart Bot", "set:restart", "danger", "🔄"), ("Admin Panel", "adm:home", "primary", "⚙️")],
    )


# ════════════════════════════════════════════════════════════════
#  9. CORE HELPERS (access control, membership, state)
# ════════════════════════════════════════════════════════════════

def is_owner(uid: Optional[int]) -> bool:
    return uid is not None and uid == OWNER_ID


def db_of(ctx: Ctx) -> Database:
    return ctx.application.bot_data["db"]


def get_state(ctx: Ctx) -> Optional[dict[str, Any]]:
    return ctx.user_data.get("state")


def set_state(ctx: Ctx, name: str, **data: Any) -> None:
    ctx.user_data["state"] = {"name": name, **data}


def clear_state(ctx: Ctx) -> None:
    ctx.user_data.pop("state", None)


def norm_label(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


async def join_required(ctx: Ctx) -> bool:
    return (await db_of(ctx).run(db_of(ctx).get_setting, "join_required")) == "1"


async def check_membership(ctx: Ctx, uid: int, force: bool = False) -> str:
    """Returns 'ok', 'no' or 'error'. `force=True` always asks Telegram (fresh check)."""
    cache: dict[int, float] = ctx.application.bot_data.setdefault("mcache", {})
    now = time.monotonic()
    if not force and now - cache.get(uid, -1e9) < MEMBERSHIP_CACHE_TTL:
        return "ok"
    try:
        member = await _tg(lambda: ctx.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=uid))
    except BadRequest as exc:
        low = str(exc).lower()
        if "user not found" in low or "participant_id_invalid" in low:
            cache.pop(uid, None)
            return "no"
        log.error("Membership check BadRequest: %s (is the bot an admin of the channel?)", exc)
        await _warn_owner(ctx, "Channel check failed. Make sure the bot is an ADMIN of the channel "
                               "and CHANNEL_ID is correct.")
        return "error"
    except Forbidden as exc:
        log.error("Membership check Forbidden: %s", exc)
        await _warn_owner(ctx, "Bot cannot read channel members. Add it as ADMIN of the channel.")
        return "error"
    except TelegramError as exc:
        log.warning("Membership check network/API error: %s", exc)
        return "error"

    status = member.status
    if status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
        ok = True
    elif status == ChatMemberStatus.RESTRICTED:
        ok = bool(getattr(member, "is_member", False))      # restricted but still in the channel
    else:                                                    # left / kicked
        ok = False
    if ok:
        cache[uid] = now
        return "ok"
    cache.pop(uid, None)
    return "no"


async def _warn_owner(ctx: Ctx, text: str) -> None:
    """Tell the owner about a config problem (at most once per hour)."""
    last = ctx.application.bot_data.get("warned", 0.0)
    if time.monotonic() - last < 3600:
        return
    ctx.application.bot_data["warned"] = time.monotonic()
    await Ui.send(ctx.bot, OWNER_ID, f"⚠️ <b>CONFIGURATION WARNING</b>\n\n🔧 {esc(text)}")


async def show_join(ctx: Ctx, chat_id: int, mid: Optional[int] = None, mode: str = "join") -> None:
    text = {"join": Msg.join, "failed": Msg.verify_failed, "error": Msg.verify_error}[mode]()
    await Ui.show(ctx.bot, chat_id, mid, text, kb_join())


async def gate(update: Update, ctx: Ctx) -> bool:
    """Central access check: private chat, not banned, channel member (fresh-ish)."""
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != ChatType.PRIVATE:
        return False
    db = db_of(ctx)
    try:
        await db.run(db.touch_user, user.id, user.username, user.first_name)
        if is_owner(user.id):
            return True
        if await db.run(db.is_banned, user.id):
            await Ui.send(ctx.bot, chat.id, Msg.banned())
            return False
    except sqlite3.Error as exc:
        log.error("DB error in gate: %s", exc)
        await Ui.send(ctx.bot, chat.id, Msg.error())
        return False
    if await join_required(ctx):
        status = await check_membership(ctx, user.id)
        if status != "ok":
            await db.run(db.set_verified, user.id, False) if status == "no" else None
            await show_join(ctx, chat.id, mode="join" if status == "no" else "error")
            return False
    return True


async def send_menu(ctx: Ctx, chat_id: int, uid: int) -> None:
    await Ui.send(ctx.bot, chat_id, Msg.menu(is_owner(uid)), kb_main(is_owner(uid)))


# ════════════════════════════════════════════════════════════════
#  10. USER HANDLERS
# ════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: Ctx) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != ChatType.PRIVATE:
        return
    clear_state(ctx)
    db = db_of(ctx)
    try:
        await db.run(db.touch_user, user.id, user.username, user.first_name)
        if not is_owner(user.id) and await db.run(db.is_banned, user.id):
            await Ui.send(ctx.bot, chat.id, Msg.banned())
            return
    except sqlite3.Error as exc:
        log.error("DB error in /start: %s", exc)
        await Ui.send(ctx.bot, chat.id, Msg.error())
        return

    if is_owner(user.id) or not await join_required(ctx):
        await send_menu(ctx, chat.id, user.id)
        return
    status = await check_membership(ctx, user.id, force=True)      # fresh check on /start
    if status == "ok":
        await db.run(db.set_verified, user.id, True)
        await Ui.send(ctx.bot, chat.id, Msg.verified())
        await send_menu(ctx, chat.id, user.id)
    else:
        await db.run(db.set_verified, user.id, False) if status == "no" else None
        await show_join(ctx, chat.id, mode="join" if status == "no" else "error")


async def cb_verify(update: Update, ctx: Ctx) -> None:
    q = update.callback_query
    user = q.from_user
    chat_id = user.id
    mid = q.message.message_id if q.message else None
    db = db_of(ctx)
    try:
        await db.run(db.touch_user, user.id, user.username, user.first_name)
    except sqlite3.Error as exc:
        log.error("DB error in verify: %s", exc)
    status = await check_membership(ctx, user.id, force=True)      # ALWAYS a fresh check
    if status == "ok":
        await safe_answer(q, "✅ Verified!")
        await db.run(db.set_verified, user.id, True)
        await Ui.show(ctx.bot, chat_id, mid, Msg.verified(), None)
        await send_menu(ctx, chat_id, user.id)
    elif status == "no":
        await safe_answer(q, "❌ You have not joined the channel yet.", alert=True)
        await db.run(db.set_verified, user.id, False)
        await show_join(ctx, chat_id, mid, mode="failed")
    else:
        await safe_answer(q, "⚠️ Could not verify right now. Try again shortly.", alert=True)
        await show_join(ctx, chat_id, mid, mode="error")


async def do_hunt(ctx: Ctx, chat_id: int, uid: int) -> None:
    if ctx.user_data.get("busy"):
        return
    db = db_of(ctx)
    try:
        cooldown = int(await db.run(db.get_setting, "cooldown") or 0)
        animate = (await db.run(db.get_setting, "animation")) == "1"
    except (sqlite3.Error, ValueError):
        cooldown, animate = HUNT_COOLDOWN_SECONDS, True
    last: dict[int, float] = ctx.application.bot_data.setdefault("hunt_last", {})
    wait = cooldown - (time.monotonic() - last.get(uid, -1e9))
    if wait > 0 and not is_owner(uid):
        await Ui.send(ctx.bot, chat_id, Msg.cooldown(math.ceil(wait)))
        return

    ctx.user_data["busy"] = True
    last[uid] = time.monotonic()
    try:
        mid: Optional[int] = None
        if animate:
            for i in range(3):                                    # frames 1-3
                mid = await Ui.show(ctx.bot, chat_id, mid, Msg.hunt_frame(i), None)
                await asyncio.sleep(ANIMATION_DELAY)
        try:
            acc = await db.run(db.assign_account, uid)            # atomic, transaction-safe
        except sqlite3.Error as exc:
            log.error("assign_account failed: %s", exc)
            await Ui.show(ctx.bot, chat_id, mid, Msg.error(), KB_RETRY)
            return
        if not acc:
            await Ui.show(ctx.bot, chat_id, mid, Msg.no_stock(), KB_RETRY)
            return
        if animate:
            for i in (3, 4):                                      # frames 4-5
                mid = await Ui.show(ctx.bot, chat_id, mid, Msg.hunt_frame(i), None)
                await asyncio.sleep(ANIMATION_DELAY)
        service = await db.run(db.get_setting, "service_name")
        credit = await db.run(db.get_setting, "bot_credit")
        shown = await Ui.show(ctx.bot, chat_id, mid, Msg.delivery(acc, service, credit), KB_HUNT_AGAIN)
        if shown is None:
            log.error("Delivery message failed for assignment #%s (user %s) — visible in /History", acc["assign_id"], uid)
        else:
            log.info("Account #%s delivered to user %s (assignment #%s)", acc["id"], uid, acc["assign_id"])
    finally:
        ctx.user_data["busy"] = False


async def show_my_account(ctx: Ctx, chat_id: int, uid: int) -> None:
    db = db_of(ctx)
    try:
        u = await db.run(db.get_user, uid) or {"user_id": uid}
        received = await db.run(db.user_received, uid)
    except sqlite3.Error as exc:
        log.error("my_account DB error: %s", exc)
        await Ui.send(ctx.bot, chat_id, Msg.error())
        return
    await Ui.send(ctx.bot, chat_id, Msg.my_account(u, received, is_owner(uid)), KB_ACCOUNT)


async def show_history(ctx: Ctx, chat_id: int, uid: int, mid: Optional[int] = None) -> None:
    db = db_of(ctx)
    try:
        rows = await db.run(db.user_history, uid, 8)
    except sqlite3.Error as exc:
        log.error("history DB error: %s", exc)
        await Ui.send(ctx.bot, chat_id, Msg.error())
        return
    await Ui.show(ctx.bot, chat_id, mid, Msg.history(rows), kb_history(rows) if rows else KB_RETRY)


async def show_updates(ctx: Ctx, chat_id: int) -> None:
    db = db_of(ctx)
    text = EMOJI.ensure_line_emoji(esc(await db.run(db.get_setting, "updates_text")), "📢")
    await Ui.send(ctx.bot, chat_id, f"📢 <b>UPDATES</b>\n\n{text}")


async def view_history_item(ctx: Ctx, chat_id: int, uid: int, assign_id: int, mid: Optional[int]) -> None:
    db = db_of(ctx)
    acc = await db.run(db.get_assignment, assign_id, uid)          # ownership enforced in SQL
    if not acc:
        await Ui.send(ctx.bot, chat_id, "⚠️ <b>Record not found.</b>\n\n📜 Open History again.")
        return
    service = await db.run(db.get_setting, "service_name")
    credit = await db.run(db.get_setting, "bot_credit")
    await Ui.send(ctx.bot, chat_id, Msg.delivery(acc, service, credit),
                  kb_rows([("History", "hist:list", "primary", "📜"), ("Hunt Account", "hunt", "success", "🔎")]))


# ════════════════════════════════════════════════════════════════
#  11. ADMIN HANDLERS
# ════════════════════════════════════════════════════════════════

async def admin_home(ctx: Ctx, chat_id: int, mid: Optional[int] = None) -> None:
    db = db_of(ctx)
    st = await db.run(db.quick_stats)
    await Ui.show(ctx.bot, chat_id, mid, Msg.admin_home(st), KB_ADMIN)


async def admin_list(ctx: Ctx, chat_id: int, mid: Optional[int], kind: str, page: int) -> None:
    db = db_of(ctx)
    page = max(page, 0)
    if kind == "stock":
        rows, total = await db.run(db.list_available, page * PAGE_SIZE, PAGE_SIZE)
        title = "📦 <b>ACCOUNT STOCK</b>"
        lines = [f"🛍 <b>#{r['id']}</b> · {esc(mask_email(r['email']))} · {esc(r.get('plan') or 'N/A')} · "
                 f"{esc(r.get('country') or 'N/A')}" for r in rows]
        empty = "😔 The stock is empty."
    else:
        rows, total = await db.run(db.list_distributed, page * PAGE_SIZE, PAGE_SIZE)
        title = "📤 <b>DISTRIBUTED ACCOUNTS</b>"
        lines = []
        for r in rows:
            who = f"@{esc(r['username'])}" if r.get("username") else f"<code>{r['user_id']}</code>"
            lines.append(f"📌 <b>#{r['account_id']}</b> · {esc(mask_email(r['email']))} → {who} · "
                         f"{esc(r['assigned_at'][:16])}")
        empty = "😔 Nothing has been distributed yet."
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages - 1)
    body = "\n".join(lines) if lines else empty
    text = f"{title}\n\n📊 Total: <b>{total}</b>\n\n{body}\n\n📄 Page {page + 1}/{pages}"
    await Ui.show(ctx.bot, chat_id, mid, text, kb_pager(kind, page, pages))


async def admin_users(ctx: Ctx, chat_id: int, mid: Optional[int]) -> None:
    db = db_of(ctx)
    st = await db.run(db.full_stats)
    rows, _ = await db.run(db.list_users, 0, 8)
    lines = []
    for r in rows:
        tag = "🚫" if r["is_banned"] else ("✅" if r["is_verified"] else "⏳")
        name = f"@{esc(r['username'])}" if r.get("username") else esc(r.get("first_name") or "N/A")
        lines.append(f"{tag} <code>{r['user_id']}</code> · {name}")
    text = (f"👥 <b>USER MANAGEMENT</b>\n\n"
            f"👤 Total users: <b>{st['users']}</b>\n"
            f"✅ Verified: <b>{st['users_verified']}</b>\n"
            f"🚫 Banned: <b>{st['users_banned']}</b>\n\n"
            f"📌 <b>Latest users:</b>\n" + ("\n".join(lines) if lines else "😔 No users yet."))
    await Ui.show(ctx.bot, chat_id, mid, text, KB_USERS)


async def admin_stats(ctx: Ctx, chat_id: int, mid: Optional[int]) -> None:
    db = db_of(ctx)
    s = await db.run(db.full_stats)
    text = (f"📊 <b>STATISTICS</b>\n\n"
            f"📦 Total accounts: <b>{s['total']}</b>\n"
            f"✅ Available: <b>{s['available']}</b>\n"
            f"📤 Distributed: <b>{s['distributed']}</b>\n\n"
            f"👥 Users: <b>{s['users']}</b> (verified {s['users_verified']}, banned {s['users_banned']})\n"
            f"🆕 New users (24h): <b>{s['users_24h']}</b>\n\n"
            f"🔥 Delivered (24h): <b>{s['dist_24h']}</b>\n"
            f"📈 Delivered (7d): <b>{s['dist_7d']}</b>")
    await Ui.show(ctx.bot, chat_id, mid, text, KB_BACK_ADMIN)


async def admin_settings(ctx: Ctx, chat_id: int, mid: Optional[int]) -> None:
    db = db_of(ctx)
    g = lambda k: db.get_setting(k)                                    # noqa: E731
    service, credit, cooldown, join, upd = await asyncio.gather(*[
        db.run(g, k) for k in ("service_name", "bot_credit", "cooldown", "join_required", "updates_text")])
    text = (f"⚙️ <b>SETTINGS</b>\n\n"
            f"🚀 Animation: <b>{'ON' if Runtime.animation else 'OFF'}</b>\n"
            f"✨ Premium emoji: <b>{'ON' if Runtime.rich else 'OFF'}</b>\n"
            f"🔒 Channel join required: <b>{'ON' if join == '1' else 'OFF'}</b>\n"
            f"⏳ Hunt cooldown: <b>{esc(cooldown)}s</b>\n"
            f"👑 Service name: <b>{esc(service)}</b>\n"
            f"🤖 Bot credit: <b>{esc(credit)}</b>\n"
            f"📢 Updates text: <b>{len(upd)}</b> characters")
    await Ui.show(ctx.bot, chat_id, mid, text, kb_settings())


# ---- guided input flows (owner) ------------------------------------------

async def ask_field(ctx: Ctx, chat_id: int, mid: Optional[int], step: int) -> None:
    key, label, icon, required = ACCOUNT_FIELDS[step]
    hint = "🔴 Required." if required else "⏩ Optional — send <code>-</code> or press Skip."
    text = (f"➕ <b>ADD ACCOUNT</b> — Step {step + 1}/{len(ACCOUNT_FIELDS)}\n\n"
            f"{icon} Send the <b>{label}</b>:\n\n{hint}")
    new_mid = await Ui.show(ctx.bot, chat_id, mid, text, KB_CANCEL if required else KB_SKIP)
    st = get_state(ctx)
    if st is not None:
        st["mid"] = new_mid


async def add_field_value(ctx: Ctx, chat_id: int, value: Optional[str]) -> None:
    st = get_state(ctx)
    if not st or st["name"] != "add":
        return
    step = st["step"]
    key, label, icon, required = ACCOUNT_FIELDS[step]
    value = clean(value or "")
    if value == "-":
        value = ""
    if required and not value:
        await Ui.send(ctx.bot, chat_id, f"⚠️ <b>{label} is required.</b>\n\n🔄 Please send it again.", KB_CANCEL)
        return
    if key == "email" and not EMAIL_RE.match(value):
        await Ui.send(ctx.bot, chat_id, "❌ <b>Invalid email.</b>\n\n🔄 Send a valid email address.", KB_CANCEL)
        return
    if key == "login_url" and value and not URL_RE.match(value):
        await Ui.send(ctx.bot, chat_id, "❌ <b>Invalid URL.</b>\n\n🔄 It must start with http:// or https://", KB_SKIP)
        return
    st["data"][key] = value
    if step + 1 < len(ACCOUNT_FIELDS):
        st["step"] = step + 1
        await ask_field(ctx, chat_id, None, st["step"])
        return
    acc, reason = validate_account(st["data"])
    clear_state(ctx)
    if not acc:
        await Ui.send(ctx.bot, chat_id, f"❌ <b>Account rejected:</b> {esc(reason)}", KB_ADD_DONE)
        return
    db = db_of(ctx)
    try:
        added = await db.run(db.add_account, acc)
        stock = (await db.run(db.quick_stats))["available"]
    except sqlite3.Error as exc:
        log.error("add_account DB error: %s", exc)
        await Ui.send(ctx.bot, chat_id, Msg.error(), KB_ADD_DONE)
        return
    if added:
        await Ui.send(ctx.bot, chat_id, f"✅ <b>ACCOUNT ADDED</b>\n\n"
                                        f"📧 {esc(mask_email(acc['email']))}\n"
                                        f"📦 Available stock: <b>{stock}</b>", KB_ADD_DONE)
    else:
        await Ui.send(ctx.bot, chat_id, "⚠️ <b>DUPLICATE ACCOUNT</b>\n\n📧 This email already exists in the stock.",
                      KB_ADD_DONE)


async def handle_state_text(update: Update, ctx: Ctx, st: dict[str, Any]) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id
    text = (msg.text or "").strip()
    name = st["name"]
    db = db_of(ctx)

    if name == "add":
        await add_field_value(ctx, chat_id, text)
        if st["step"] >= 1:                                            # hide the password from the chat
            try:
                await msg.delete()
            except TelegramError:
                pass
    elif name in ("ban", "unban"):
        if not re.fullmatch(r"\d{1,15}", text):
            await Ui.send(ctx.bot, chat_id, "❌ <b>Invalid user ID.</b>\n\n🔄 Send numbers only.", KB_CANCEL)
            return
        uid = int(text)
        if is_owner(uid):
            await Ui.send(ctx.bot, chat_id, "⛔ <b>You cannot ban the owner.</b>", KB_BACK_ADMIN)
            clear_state(ctx)
            return
        ok = await db.run(db.set_banned, uid, name == "ban")
        clear_state(ctx)
        label = "banned" if name == "ban" else "unbanned"
        await Ui.send(ctx.bot, chat_id,
                      f"✅ <b>User {label}.</b>\n\n👤 <code>{uid}</code>" if ok else
                      "⚠️ <b>User not found.</b>\n\n👥 They must have started the bot first.", KB_USERS)
    elif name == "remove":
        if not re.fullmatch(r"\d{1,12}", text):
            await Ui.send(ctx.bot, chat_id, "❌ <b>Invalid account ID.</b>\n\n🔄 Send the number shown in Account Stock.",
                          KB_CANCEL)
            return
        acc = await db.run(db.get_account, int(text))
        if not acc:
            await Ui.send(ctx.bot, chat_id, "⚠️ <b>Account not found.</b>\n\n🔄 Check the ID and try again.", KB_CANCEL)
            return
        if acc["status"] != "available":
            clear_state(ctx)
            await Ui.send(ctx.bot, chat_id, "⛔ <b>Already distributed.</b>\n\n📜 Distributed accounts stay in history.",
                          KB_BACK_ADMIN)
            return
        clear_state(ctx)
        await Ui.send(ctx.bot, chat_id, f"🗑️ <b>REMOVE ACCOUNT #{acc['id']}?</b>\n\n"
                                        f"📧 {esc(mask_email(acc['email']))}\n"
                                        f"💎 {esc(acc.get('plan') or 'N/A')}\n\n⚠️ This cannot be undone.",
                      kb_remove_confirm(acc["id"]))
    elif name == "broadcast":
        if not text:
            return
        ctx.user_data["draft"] = text[:3500]
        clear_state(ctx)
        preview = EMOJI.ensure_line_emoji(esc(text[:3500]), "📢")
        await Ui.send(ctx.bot, chat_id, f"📢 <b>BROADCAST PREVIEW</b>\n\n{preview}\n\n🚀 Send this to all verified users?",
                      KB_BC_CONFIRM)
    elif name == "setedit":
        await save_setting_edit(ctx, chat_id, st["key"], text)
    else:
        clear_state(ctx)


async def save_setting_edit(ctx: Ctx, chat_id: int, key: str, value: str) -> None:
    limits = {"service_name": 40, "bot_credit": 60, "updates_text": 1000}
    db = db_of(ctx)
    if key == "cooldown":
        if not re.fullmatch(r"\d{1,4}", value) or int(value) > 3600:
            await Ui.send(ctx.bot, chat_id, "❌ <b>Invalid value.</b>\n\n⏳ Send seconds between 0 and 3600.", KB_CANCEL)
            return
    else:
        value = clean(value) if key != "updates_text" else value.strip()
        if not value or len(value) > limits.get(key, 100):
            await Ui.send(ctx.bot, chat_id, f"❌ <b>Invalid text.</b>\n\n📝 Maximum {limits.get(key, 100)} characters.",
                          KB_CANCEL)
            return
    await db.run(db.set_setting, key, value)
    clear_state(ctx)
    await Ui.send(ctx.bot, chat_id, "✅ <b>SETTING SAVED</b>\n\n⚙️ Your change is live.", KB_rows_settings())


def KB_rows_settings() -> KbFactory:
    return kb_rows([("Settings", "adm:settings", "primary", "⚙️"), ("Admin Panel", "adm:home", "primary", "⚙️")])


async def do_broadcast(ctx: Ctx, chat_id: int, mid: Optional[int]) -> None:
    text = ctx.user_data.pop("draft", None)
    if not text:
        await Ui.show(ctx.bot, chat_id, mid, "⚠️ <b>Nothing to send.</b>\n\n📢 Start a new broadcast.", KB_BACK_ADMIN)
        return
    db = db_of(ctx)
    targets = await db.run(db.broadcast_targets)
    body = "📢 <b>ANNOUNCEMENT</b>\n\n" + EMOJI.ensure_line_emoji(esc(text), "📢")
    sent = failed = 0
    await Ui.show(ctx.bot, chat_id, mid, f"🚀 <b>Broadcasting to {len(targets)} users...</b>", None)
    for i, uid in enumerate(targets, 1):
        msg = await Ui.send(ctx.bot, uid, body)
        if msg:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)                                      # stay far below flood limits
        if i % 100 == 0:
            await Ui.show(ctx.bot, chat_id, mid, f"🚀 <b>Broadcasting...</b>\n\n📊 {i}/{len(targets)}", None)
    await Ui.show(ctx.bot, chat_id, mid, f"📢 <b>BROADCAST COMPLETE</b>\n\n✅ Delivered: <b>{sent}</b>\n"
                                          f"❌ Failed: <b>{failed}</b>\n\n📊 Total: <b>{len(targets)}</b>", KB_BACK_ADMIN)


async def import_document(update: Update, ctx: Ctx) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id
    doc = msg.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await Ui.send(ctx.bot, chat_id, "❌ <b>INVALID FILE</b>\n\n📄 Please upload a <b>.txt</b> file.", KB_BULK_WAIT)
        return
    if doc.file_size and doc.file_size > MAX_TXT_BYTES:
        await Ui.send(ctx.bot, chat_id, "❌ <b>FILE TOO LARGE</b>\n\n📄 Maximum size is 5 MB.", KB_BULK_WAIT)
        return
    status = await Ui.send(ctx.bot, chat_id, "⏳ <b>Processing your file...</b>\n\n📦 Please wait.")
    try:
        tg_file = await _tg(lambda: doc.get_file())
        data = bytes(await _tg(lambda: tg_file.download_as_bytearray()))
    except TelegramError as exc:
        log.error("File download failed: %s", exc)
        await Ui.send(ctx.bot, chat_id, "❌ <b>DOWNLOAD FAILED</b>\n\n🔄 Telegram could not deliver the file. Try again.",
                      KB_BULK_WAIT)
        return
    if not data.strip():
        await Ui.send(ctx.bot, chat_id, "⚠️ <b>EMPTY FILE</b>\n\n📄 The file has no content.", KB_BULK_WAIT)
        return
    if b"\x00" in data[:2048]:
        await Ui.send(ctx.bot, chat_id, "❌ <b>INVALID FILE</b>\n\n📄 This does not look like a text file.", KB_BULK_WAIT)
        return
    valid, invalid, total = parse_txt(data)
    if total == 0:
        await Ui.send(ctx.bot, chat_id, "⚠️ <b>EMPTY FILE</b>\n\n📄 No account lines were found.", KB_BULK_WAIT)
        return
    db = db_of(ctx)
    try:
        added, dups = await db.run(db.bulk_insert, valid)
        stock = (await db.run(db.quick_stats))["available"]
    except sqlite3.Error as exc:
        log.error("bulk_insert failed: %s", exc)
        await Ui.send(ctx.bot, chat_id, Msg.error(), KB_BULK_WAIT)
        return
    extra = ""
    if invalid:
        shown = ", ".join(str(n) for n in invalid[:10]) + (" …" if len(invalid) > 10 else "")
        extra = f"\n\n🚫 Invalid line numbers: {shown}"
    log.info("Bulk import: added=%d dup=%d invalid=%d", added, dups, len(invalid))
    clear_state(ctx)
    await Ui.send(ctx.bot, chat_id, f"📦 <b>BULK IMPORT COMPLETE</b>\n\n"
                                    f"✅ Added: <b>{added}</b>\n"
                                    f"⚠️ Duplicates: <b>{dups}</b>\n"
                                    f"❌ Invalid: <b>{len(invalid)}</b>\n\n"
                                    f"📊 Total processed: <b>{total}</b>\n"
                                    f"📦 Available stock: <b>{stock}</b>{extra}", KB_BULK_DONE)


# ════════════════════════════════════════════════════════════════
#  12. ROUTERS
# ════════════════════════════════════════════════════════════════

MENU_ROUTES = {"hunt account": "hunt", "my account": "acct", "history": "hist",
               "updates": "upd", "help": "help", "admin panel": "admin"}


async def on_text(update: Update, ctx: Ctx) -> None:
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type != ChatType.PRIVATE:
        return
    route = MENU_ROUTES.get(norm_label(msg.text or ""))
    if route:
        clear_state(ctx)
    if not await gate(update, ctx):
        return
    if route == "hunt":
        await do_hunt(ctx, chat.id, user.id)
    elif route == "acct":
        await show_my_account(ctx, chat.id, user.id)
    elif route == "hist":
        await show_history(ctx, chat.id, user.id)
    elif route == "upd":
        await show_updates(ctx, chat.id)
    elif route == "help":
        await Ui.send(ctx.bot, chat.id, Msg.help(), kb_main(is_owner(user.id)))
    elif route == "admin":
        if is_owner(user.id):
            await admin_home(ctx, chat.id)
        else:
            await Ui.send(ctx.bot, chat.id, "⛔ <b>ACCESS DENIED</b>\n\n🔒 Owner only.")
    else:
        st = get_state(ctx)
        if st and is_owner(user.id):
            await handle_state_text(update, ctx, st)
        else:
            await Ui.send(ctx.bot, chat.id, Msg.hint(), kb_main(is_owner(user.id)))


async def on_document(update: Update, ctx: Ctx) -> None:
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user or chat.type != ChatType.PRIVATE or not is_owner(user.id):
        return
    st = get_state(ctx)
    if st and st["name"] == "bulk":
        await import_document(update, ctx)
    else:
        await Ui.send(ctx.bot, chat.id, "📦 <b>Open Admin Panel → Bulk Import first</b>, then upload your file.",
                      KB_BACK_ADMIN)


async def cmd_menu(update: Update, ctx: Ctx) -> None:
    if await gate(update, ctx):
        clear_state(ctx)
        await send_menu(ctx, update.effective_chat.id, update.effective_user.id)


async def cmd_admin(update: Update, ctx: Ctx) -> None:
    user, chat = update.effective_user, update.effective_chat
    if chat and chat.type == ChatType.PRIVATE and is_owner(user.id if user else None):
        clear_state(ctx)
        await admin_home(ctx, chat.id)


async def cmd_cancel(update: Update, ctx: Ctx) -> None:
    chat = update.effective_chat
    if chat and chat.type == ChatType.PRIVATE:
        clear_state(ctx)
        ctx.user_data.pop("draft", None)
        await Ui.send(ctx.bot, chat.id, "❌ <b>CANCELLED</b>\n\n🏠 Back to the main menu.",
                      kb_main(is_owner(update.effective_user.id)))


async def cmd_restart(update: Update, ctx: Ctx) -> None:
    user = update.effective_user
    if is_owner(user.id if user else None):
        await request_restart(ctx, update.effective_chat.id)


async def request_restart(ctx: Ctx, chat_id: int) -> None:
    await Ui.send(ctx.bot, chat_id, "🔄 <b>RESTARTING...</b>\n\n⏳ Hunter will be back in a few seconds.")
    ctx.application.bot_data["restart"] = True
    ctx.application.stop_running()


OWNER_PREFIXES = ("adm:", "rm:", "bc:", "set:", "st:", "bulk:")


async def on_callback(update: Update, ctx: Ctx) -> None:
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    if data == "verify":
        await cb_verify(update, ctx)
        return
    await safe_answer(q)
    user = q.from_user
    chat_id = user.id                                   # private-chat bot: chat id == user id
    mid = q.message.message_id if q.message else None
    if not await gate(update, ctx):
        return
    if data.startswith(OWNER_PREFIXES) and not is_owner(user.id):
        await safe_answer(q, "⛔ Access denied", alert=True)
        return
    try:
        await route_callback(update, ctx, data, chat_id, mid, user.id)
    except sqlite3.Error as exc:
        log.error("DB error in callback %s: %s", data.split(":")[0], exc)
        await Ui.send(ctx.bot, chat_id, Msg.error())


async def route_callback(update: Update, ctx: Ctx, data: str, chat_id: int, mid: Optional[int], uid: int) -> None:
    parts = data.split(":")
    head = parts[0]
    db = db_of(ctx)

    if data == "hunt":
        await do_hunt(ctx, chat_id, uid)
    elif data == "hist:list":
        await show_history(ctx, chat_id, uid)
    elif head == "hist" and len(parts) == 3 and parts[1] == "view" and parts[2].isdigit():
        await view_history_item(ctx, chat_id, uid, int(parts[2]), mid)

    elif data == "st:cancel":
        clear_state(ctx)
        ctx.user_data.pop("draft", None)
        await admin_home(ctx, chat_id, mid)
    elif data == "st:skip":
        st = get_state(ctx)
        if st and st["name"] == "add":
            await add_field_value(ctx, chat_id, "-")
        else:
            await Ui.send(ctx.bot, chat_id, "⚠️ <b>This button has expired.</b>\n\n🔄 Please start again.", KB_BACK_ADMIN)

    elif data == "adm:home":
        clear_state(ctx)
        await admin_home(ctx, chat_id, mid)
    elif data == "adm:user_panel":
        clear_state(ctx)
        await send_menu(ctx, chat_id, uid)
    elif data == "adm:add":
        set_state(ctx, "add", step=0, data={}, mid=mid)
        await ask_field(ctx, chat_id, mid, 0)
    elif data == "adm:bulk":
        set_state(ctx, "bulk")
        await Ui.show(ctx.bot, chat_id, mid,
                      "📦 <b>BULK IMPORT</b>\n\n"
                      "📄 Upload a <b>.txt</b> file now.\n\n"
                      "🧾 Format (one account per line):\n"
                      "<code>email|password|username|plan|premium|country|expiry|verified|login_url</code>\n\n"
                      "✅ Only email and password are required.\n"
                      "⚠️ Duplicates and invalid lines are skipped automatically.", KB_BULK_WAIT)
    elif data == "bulk:done":
        clear_state(ctx)
        await admin_home(ctx, chat_id, mid)
    elif head == "adm" and len(parts) == 3 and parts[1] in ("stock", "dist") and parts[2].isdigit():
        await admin_list(ctx, chat_id, mid, "stock" if parts[1] == "stock" else "dist", int(parts[2]))
    elif data == "adm:users":
        await admin_users(ctx, chat_id, mid)
    elif data in ("adm:ban", "adm:unban"):
        name = data.split(":")[1]
        set_state(ctx, name)
        await Ui.show(ctx.bot, chat_id, mid, f"{'🚫' if name == 'ban' else '✅'} <b>{name.upper()} USER</b>\n\n"
                                             f"🔐 Send the Telegram user ID:", KB_CANCEL)
    elif data == "adm:stats":
        await admin_stats(ctx, chat_id, mid)
    elif data == "adm:remove":
        set_state(ctx, "remove")
        await Ui.show(ctx.bot, chat_id, mid, "🗑️ <b>REMOVE ACCOUNT</b>\n\n📦 Send the account ID "
                                             "(see Account Stock). Only undistributed accounts can be removed.", KB_CANCEL)
    elif head == "rm" and len(parts) == 3 and parts[1] == "yes" and parts[2].isdigit():
        ok = await db.run(db.remove_account, int(parts[2]))
        await Ui.show(ctx.bot, chat_id, mid,
                      "✅ <b>ACCOUNT REMOVED</b>\n\n🗑️ It has been deleted from the stock." if ok else
                      "⚠️ <b>Could not remove.</b>\n\n📦 It was already removed or distributed.", KB_BACK_ADMIN)
    elif data == "adm:bc":
        set_state(ctx, "broadcast")
        await Ui.show(ctx.bot, chat_id, mid, "📢 <b>BROADCAST</b>\n\n📝 Send the message you want to deliver to "
                                             "all verified users:", KB_CANCEL)
    elif data == "bc:yes":
        await do_broadcast(ctx, chat_id, mid)
    elif data == "bc:no":
        ctx.user_data.pop("draft", None)
        await Ui.show(ctx.bot, chat_id, mid, "❌ <b>BROADCAST CANCELLED</b>\n\n🏠 Nothing was sent.", KB_BACK_ADMIN)

    elif data == "adm:settings":
        await admin_settings(ctx, chat_id, mid)
    elif data == "set:anim":
        Runtime.animation = not Runtime.animation
        await db.run(db.set_setting, "animation", "1" if Runtime.animation else "0")
        await admin_settings(ctx, chat_id, mid)
    elif data == "set:rich":
        Runtime.rich = not Runtime.rich
        await db.run(db.set_setting, "rich_emoji", "1" if Runtime.rich else "0")
        await admin_settings(ctx, chat_id, mid)
    elif data == "set:join":
        cur = await db.run(db.get_setting, "join_required")
        await db.run(db.set_setting, "join_required", "0" if cur == "1" else "1")
        await admin_settings(ctx, chat_id, mid)
    elif head == "set" and len(parts) == 3 and parts[1] == "edit" and parts[2] in (
            "cooldown", "service_name", "bot_credit", "updates_text"):
        set_state(ctx, "setedit", key=parts[2])
        names = {"cooldown": "hunt cooldown (seconds)", "service_name": "service name",
                 "bot_credit": "bot credit", "updates_text": "updates text"}
        await Ui.show(ctx.bot, chat_id, mid, f"🖊 <b>EDIT SETTING</b>\n\n✏️ Send the new <b>{names[parts[2]]}</b>:", KB_CANCEL)
    elif data == "set:restart":
        await request_restart(ctx, chat_id)
    else:
        await Ui.send(ctx.bot, chat_id, "⚠️ <b>This button has expired.</b>\n\n🏠 Open the menu and try again.",
                      kb_main(is_owner(uid)))


async def on_error(update: object, ctx: Ctx) -> None:
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut, RetryAfter)):
        log.warning("Transient Telegram/network error: %s", err)
        return
    log.error("Unhandled exception: %r", err, exc_info=err if isinstance(err, BaseException) else None)
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await Ui.send(ctx.bot, update.effective_chat.id, Msg.error())


# ════════════════════════════════════════════════════════════════
#  13. STARTUP
# ════════════════════════════════════════════════════════════════

async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    log.info("Hunter started as @%s (id %s)", me.username, me.id)
    db: Database = app.bot_data["db"]
    Runtime.animation = db.get_setting("animation") == "1"
    Runtime.rich = db.get_setting("rich_emoji") == "1"
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Start / verify membership"), BotCommand("menu", "Show the main menu"),
            BotCommand("cancel", "Cancel the current action"), BotCommand("admin", "Admin panel (owner)"),
            BotCommand("restart", "Restart the bot (owner)")])
    except TelegramError as exc:
        log.warning("set_my_commands failed: %s", exc)
    try:
        member = await app.bot.get_chat_member(CHANNEL_ID, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            log.warning("Bot is NOT an admin of the channel — membership checks will fail!")
            await Ui.send(app.bot, OWNER_ID, "⚠️ <b>CONFIGURATION WARNING</b>\n\n🔧 Add the bot as an "
                                             "<b>administrator</b> of the channel, otherwise verification cannot work.")
    except TelegramError as exc:
        log.warning("Could not inspect channel membership of the bot: %s", exc)
    await Ui.send(app.bot, OWNER_ID, "🚀 <b>HUNTER ONLINE</b>\n\n✅ The bot is running.\n⚙️ Use /admin to open the panel.")


def build_application(db: Database) -> Application:
    app = (ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True)
           .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(20)
           .get_updates_connect_timeout(20).get_updates_read_timeout(30).post_init(post_init).build())
    app.bot_data["db"] = db
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("menu", cmd_menu, filters=private))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CommandHandler("restart", cmd_restart, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


# ════════════════════════════════════════════════════════════════
#  HEALTH SERVER  (for Render / UptimeRobot — returns 200 "OK")
# ════════════════════════════════════════════════════════════════
# Ping   https://<your-service>.onrender.com/health   with UptimeRobot.
# Render provides the PORT environment variable automatically.
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEALTH_ENABLED = True
HEALTH_HOST = "0.0.0.0"
HEALTH_PORT = int(os.environ.get("PORT", "10000"))


class _HealthHandler(BaseHTTPRequestHandler):
    def _reply(self, send_body: bool) -> None:
        ok = self.path.split("?", 1)[0] in ("/", "/health", "/healthz")
        body = b"OK" if ok else b"Not Found"
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self) -> None:          # noqa: N802
        self._reply(True)

    def do_HEAD(self) -> None:         # noqa: N802  (UptimeRobot may use HEAD)
        self._reply(False)

    def log_message(self, *args: Any) -> None:
        pass                            # keep the console/log file quiet


def start_health_server() -> None:
    """Start the tiny HTTP server in a background thread. Never crashes the bot."""
    if not HEALTH_ENABLED:
        return
    try:
        server = ThreadingHTTPServer((HEALTH_HOST, HEALTH_PORT), _HealthHandler)
    except OSError as exc:
        log.warning("Health server could not start on port %s: %s", HEALTH_PORT, exc)
        return
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    log.info("Health server listening on %s:%s (GET /health -> 200 OK)", HEALTH_HOST, HEALTH_PORT)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
        sys.exit("❌ Set BOT_TOKEN at the top of hunter.py first.")
    if OWNER_ID == 123456789:
        log.warning("OWNER_ID is still the example value — set your real Telegram ID!")
    start_health_server()
    init_emoji()
    db = Database(DB_FILE)
    backoff = 5
    try:
        while True:
            app = build_application(db)
            try:
                app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True,
                                close_loop=False)
            except InvalidToken:
                log.critical("BOT_TOKEN is invalid. Check it with @BotFather.")
                sys.exit(1)
            except (NetworkError, TimedOut, OSError) as exc:
                log.error("Network problem: %s — retrying in %ss", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if app.bot_data.get("restart"):
                log.info("Restarting process…")
                db.close()
                time.sleep(1)
                os.execv(sys.executable, [sys.executable] + sys.argv)
            break
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
        log.info("Hunter stopped.")


if __name__ == "__main__":
    main()
