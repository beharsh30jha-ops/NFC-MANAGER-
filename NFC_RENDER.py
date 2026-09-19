import os
import re
import sqlite3
import logging
import threading
from datetime import datetime, timedelta, timezone
from html import escape

from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ChatPermissions,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ChatMemberHandler, filters,
)

# ============================================================
# NFC — serious Telegram group management bot
# No games / economy / social reaction commands.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()  # optional numeric Telegram user ID
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@").strip()
INTRO_GIF = os.getenv("INTRO_GIF", "").strip()  # Telegram file_id or direct URL
DB_FILE = os.getenv("DB_FILE", "nfc.sqlite3")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("NFC")

app_health = Flask(__name__)

@app_health.get("/")
def health():
    return "NFC is running", 200

@app_health.get("/health")
def health_check():
    return {"status": "ok", "bot": "NFC"}, 200


def run_health_server():
    app_health.run(host="0.0.0.0", port=PORT, use_reloader=False)


# -----------------------------
# Database
# -----------------------------

DB_LOCK = threading.RLock()

DEFAULT_SETTINGS = {
    # Everything is optional and OFF by default.
    "automod": False,
    "antilink": False,
    "antispam": False,
    "antiflood": False,
    "antiforward": False,
    "antibot": False,
    "antimention": False,
    "repeat_guard": False,
    "anti_channel": False,
    "anti_service": False,
    "welcome": False,
    "goodbye": False,
    "delete_joinleave": False,
    "filters": False,
    "locks": False,
    "admin_logs": False,
    "analytics": False,
}



def db():
    con = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with DB_LOCK, db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT DEFAULT '',
                rules TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id, key)
            );
            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS filters (
                chat_id INTEGER NOT NULL,
                trigger TEXT NOT NULL,
                response TEXT DEFAULT '',
                action TEXT NOT NULL DEFAULT 'delete',
                PRIMARY KEY(chat_id, trigger)
            );
            CREATE TABLE IF NOT EXISTS locks (
                chat_id INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(chat_id, content_type)
            );
            CREATE TABLE IF NOT EXISTS afk (
                user_id INTEGER PRIMARY KEY,
                reason TEXT DEFAULT '',
                since INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approved (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS notes (
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(chat_id, name)
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                actor_id INTEGER,
                target_id INTEGER,
                action TEXT NOT NULL,
                details TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stats (
                chat_id INTEGER PRIMARY KEY,
                messages INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                joins INTEGER NOT NULL DEFAULT 0,
                leaves INTEGER NOT NULL DEFAULT 0,
                warnings INTEGER NOT NULL DEFAULT 0,
                mutes INTEGER NOT NULL DEFAULT 0,
                bans INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS flood (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ts INTEGER NOT NULL
            );
            """
        )


def ensure_chat(chat_id, title=""):
    now = int(datetime.now(timezone.utc).timestamp())
    with DB_LOCK, db() as con:
        con.execute(
            "INSERT OR IGNORE INTO chats(chat_id,title,created_at) VALUES(?,?,?)",
            (chat_id, title, now),
        )
        if title:
            con.execute("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))
        con.execute("INSERT OR IGNORE INTO stats(chat_id) VALUES(?)", (chat_id,))
        for key, val in DEFAULT_SETTINGS.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(chat_id,key,value) VALUES(?,?,?)",
                (chat_id, key, int(val)),
            )


def get_setting(chat_id, key):
    ensure_chat(chat_id)
    with DB_LOCK, db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key)
        ).fetchone()
        return bool(row[0]) if row else bool(DEFAULT_SETTINGS.get(key, False))


def set_setting(chat_id, key, enabled):
    ensure_chat(chat_id)
    with DB_LOCK, db() as con:
        con.execute(
            "INSERT INTO settings(chat_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value",
            (chat_id, key, int(bool(enabled))),
        )


def increment_stat(chat_id, field, amount=1):
    allowed = {"messages", "deleted", "joins", "leaves", "warnings", "mutes", "bans"}
    if field not in allowed:
        return
    ensure_chat(chat_id)
    with DB_LOCK, db() as con:
        con.execute(f"UPDATE stats SET {field}={field}+? WHERE chat_id=?", (amount, chat_id))


def add_log(chat_id, actor_id, action, target_id=None, details=""):
    if not get_setting(chat_id, "admin_logs"):
        return
    with DB_LOCK, db() as con:
        con.execute(
            "INSERT INTO logs(chat_id,actor_id,target_id,action,details,created_at) VALUES(?,?,?,?,?,?)",
            (chat_id, actor_id, target_id, action, details, int(datetime.now(timezone.utc).timestamp())),
        )


def get_warnings(chat_id, user_id):
    with DB_LOCK, db() as con:
        row = con.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        return int(row[0]) if row else 0


def set_warnings(chat_id, user_id, count):
    with DB_LOCK, db() as con:
        con.execute(
            "INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count",
            (chat_id, user_id, max(0, count)),
        )


def get_rules(chat_id):
    ensure_chat(chat_id)
    with DB_LOCK, db() as con:
        row = con.execute("SELECT rules FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else ""


def set_rules(chat_id, text):
    ensure_chat(chat_id)
    with DB_LOCK, db() as con:
        con.execute("UPDATE chats SET rules=? WHERE chat_id=?", (text, chat_id))


# -----------------------------
# Helpers
# -----------------------------

URL_RE = re.compile(
    r"(?i)(?:https?://|www\.)[^\s<>]+|(?:[a-z0-9-]+\.)+(?:com|net|org|in|io|me|xyz|site|online|app|dev|info|biz|ly|gg|shop|store)(?:/[^\s<>]*)?"
)
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{5,32}")


def mention(user):
    return f'<a href="tg://user?id={user.id}">{escape(user.full_name or "User")}</a>'


def target_from_reply(update):
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None


def is_group(update):
    return bool(update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


async def bot_username(context):
    me = await context.bot.get_me()
    return me.username or ""


async def is_admin(chat, user_id):
    try:
        member = await chat.get_member(user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False


async def require_admin(update):
    if not is_group(update):
        await update.effective_message.reply_text("This command is available in groups only.")
        return False
    if not await is_admin(update.effective_chat, update.effective_user.id):
        await update.effective_message.reply_text("⛔ Only group admins can use this command.")
        return False
    return True



async def can_act_on_target(chat, actor_id, target_id):
    if target_id == actor_id:
        return False, "You cannot use this action on yourself."
    try:
        target = await chat.get_member(target_id)
        if target.status in ("creator", "administrator"):
            return False, "That member is an administrator/owner."
        actor = await chat.get_member(actor_id)
        if actor.status in ("administrator", "creator"):
            # Telegram exposes can_restrict_members / can_delete_messages etc.
            return True, ""
        return False, "You need administrator permissions."
    except TelegramError:
        return False, "I could not verify that member."


def parse_duration(raw):
    if not raw:
        return None
    m = re.fullmatch(r"(\d+)\s*(s|m|h|d|w)", raw.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return max(1, seconds)


def duration_text(seconds):
    if seconds % 604800 == 0:
        return f"{seconds // 604800}w"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


async def perform_mute(chat, user_id, seconds=None):
    until = None if seconds is None else datetime.now(timezone.utc) + timedelta(seconds=seconds)
    await chat.restrict_member(
        user_id,
        permissions=ChatPermissions(
            can_send_messages=False, can_send_audios=False, can_send_documents=False,
            can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
            can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
            can_add_web_page_previews=False, can_change_info=False, can_invite_users=True,
            can_pin_messages=False,
        ),
        until_date=until,
    )


async def perform_unmute(chat, user_id):
    await chat.restrict_member(
        user_id,
        permissions=ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_change_info=True, can_invite_users=True,
            can_pin_messages=True,
        ),
    )


# -----------------------------
# Start / help / intro
# -----------------------------

HELP_TEXT = (
    "<b>🛡️ NFC MANAGEMENT</b>\n\n"
    "<b>Moderation</b>\n"
    "/warn · /warnings · /resetwarn\n"
    "/mute · /unmute · /ban · /unban · /kick\n"
    "/promote · /demote · /del · /purge · /pin · /unpin\n\n"
    "<b>Protection</b>\n"
    "/settings · /locks · /lock · /unlock\n"
    "/filter · /filters · /stop · /approve · /unapprove\n"
    "/approved · /antispam · /antilink · /antiflood\n\n"
    "<b>Group</b>\n"
    "/rules · /setrules · /delrules · /info · /admins · /stats\n"
    "/welcome · /goodbye\n\n"
    "<b>Utilities</b>\n"
    "/afk · /report · /note · /getnote · /userinfo · /id\n\n"
    "<i>Reply to a member's message for moderation actions.</i>"
)


def contact_button():
    if ADMIN_USERNAME:
        return InlineKeyboardButton("💬 CONTACT ADMIN", url=f"https://t.me/{ADMIN_USERNAME}")
    if ADMIN_ID.isdigit():
        return InlineKeyboardButton("💬 CONTACT ADMIN", url=f"tg://user?id={ADMIN_ID}")
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await context.bot.get_me()
    args = context.args
    help_requested = bool(args and args[0].lower() == "help")

    if help_requested:
        buttons = []
        c = contact_button()
        if c:
            buttons.append([c])
        await update.effective_message.reply_text(
            HELP_TEXT,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            disable_web_page_preview=True,
        )
        return

    rows = []
    if me.username:
        rows.append([InlineKeyboardButton("❓ MANAGEMENT HELP", url=f"https://t.me/{me.username}?start=help")])
    markup = InlineKeyboardMarkup(rows) if rows else None

    caption = (
        "<b>🛡️ NFC — GROUP MANAGEMENT</b>\n\n"
        "<i>Security • Moderation • Control</i>\n\n"
        "A professional management system for Telegram groups.\n"
        "Protect your group, manage members, configure filters, review admin logs, "
        "and control every feature from one place.\n\n"
        "<b>Core modules</b>\n"
        "🛡️ AutoMod  •  👮 Moderation  •  🔒 Locks\n"
        "📝 Filters  •  📋 Logs  •  📊 Analytics\n\n"
        "<i>Tap Help to view commands and management tools.</i>"
    )
    if INTRO_GIF:
        try:
            await update.effective_message.reply_animation(
                animation=INTRO_GIF,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except TelegramError as exc:
            log.warning("INTRO_GIF could not be sent: %s", exc)
    await update.effective_message.reply_text(caption, parse_mode="HTML", reply_markup=markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        me = await context.bot.get_me()
        rows = []
        if me.username:
            rows.append([InlineKeyboardButton("❓ OPEN HELP IN PM", url=f"https://t.me/{me.username}?start=help")])
        c = contact_button()
        if c:
            rows.append([c])
        await update.effective_message.reply_text(
            "🛡️ <b>NFC MANAGEMENT</b>\n\nHelp is available privately so the group chat stays clean.\nTap below to open the full management menu.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )
    else:
        c = contact_button()
        await update.effective_message.reply_text(
            HELP_TEXT,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[c]]) if c else None,
            disable_web_page_preview=True,
        )


# -----------------------------
# Settings panel
# -----------------------------

SETTING_LABELS = {
    "automod": "🛡️ AutoMod",
    "antilink": "🔗 Anti-Link",
    "antispam": "🚫 Anti-Spam",
    "antiflood": "🌊 Anti-Flood",
    "antiforward": "↪️ Anti-Forward",
    "antibot": "🤖 Anti-Bot",
    "antimention": "📢 Anti-Mention",
    "repeat_guard": "🔁 Repeat Guard",
    "anti_channel": "📣 Anti-Channel",
    "anti_service": "🧹 Anti-Service",
    "welcome": "👋 Welcome",
    "goodbye": "🚪 Goodbye",
    "delete_joinleave": "🧹 Join/Leave Cleanup",
    "filters": "📝 Filters",
    "locks": "🔒 Locks",
    "admin_logs": "📋 Admin Logs",
    "analytics": "📊 Analytics",
}


def settings_markup(chat_id):
    rows = []
    for key in DEFAULT_SETTINGS:
        state = "ON" if get_setting(chat_id, key) else "OFF"
        rows.append([InlineKeyboardButton(f"{SETTING_LABELS[key]}  {state}", callback_data=f"set:{key}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="settings:refresh")])
    return InlineKeyboardMarkup(rows)


async def settings_command(update, context):
    if not await require_admin(update):
        return
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text(
        "<b>⚙️ NFC GROUP SETTINGS</b>\n\n"
        "Every feature is independently configurable.\n"
        "Automation is OFF by default.",
        parse_mode="HTML",
        reply_markup=settings_markup(chat_id),
    )


async def settings_callback(update, context):
    q = update.callback_query
    await q.answer()
    if not q.message or not q.message.chat:
        return
    if not await is_admin(q.message.chat, q.from_user.id):
        await q.answer("Only group admins can change settings.", show_alert=True)
        return
    chat_id = q.message.chat.id
    if q.data == "settings:refresh":
        await q.edit_message_reply_markup(settings_markup(chat_id))
        return
    if q.data.startswith("set:"):
        key = q.data.split(":", 1)[1]
        if key not in DEFAULT_SETTINGS:
            return
        new_state = not get_setting(chat_id, key)
        set_setting(chat_id, key, new_state)
        add_log(chat_id, q.from_user.id, "setting", details=f"{key}={new_state}")
        await q.edit_message_reply_markup(settings_markup(chat_id))


# -----------------------------
# Moderation commands
# -----------------------------


async def warn(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member you want to warn.")
        return
    ok, reason = await can_act_on_target(update.effective_chat, update.effective_user.id, target.id)
    if not ok:
        await update.effective_message.reply_text(f"⛔ {reason}")
        return
    chat_id = update.effective_chat.id
    count = get_warnings(chat_id, target.id) + 1
    set_warnings(chat_id, target.id, count)
    increment_stat(chat_id, "warnings")
    add_log(chat_id, update.effective_user.id, "warn", target.id, f"count={count}")
    await update.effective_message.reply_text(
        f"⚠️ <b>Warning issued</b>\n\n👤 {mention(target)}\n📊 Warnings: <b>{count}</b>",
        parse_mode="HTML",
    )


async def warnings(update, context):
    target = target_from_reply(update) or update.effective_user
    count = get_warnings(update.effective_chat.id, target.id)
    await update.effective_message.reply_text(
        f"⚠️ {mention(target)} has <b>{count}</b> warning(s).", parse_mode="HTML"
    )


async def resetwarn(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member.")
        return
    set_warnings(update.effective_chat.id, target.id, 0)
    add_log(update.effective_chat.id, update.effective_user.id, "resetwarn", target.id)
    await update.effective_message.reply_text(f"✅ Warnings reset for {mention(target)}.", parse_mode="HTML")


async def mute(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to a member. Optional duration: /mute 1h")
        return
    ok, reason = await can_act_on_target(update.effective_chat, update.effective_user.id, target.id)
    if not ok:
        await update.effective_message.reply_text(f"⛔ {reason}")
        return
    raw = context.args[0] if context.args else None
    seconds = parse_duration(raw) if raw else None
    if raw and seconds is None:
        await update.effective_message.reply_text("Use duration like 10m, 2h, 7d or 1w.")
        return
    try:
        await perform_mute(update.effective_chat, target.id, seconds)
        increment_stat(update.effective_chat.id, "mutes")
        add_log(update.effective_chat.id, update.effective_user.id, "mute", target.id, duration_text(seconds) if seconds else "permanent")
        suffix = f" for <b>{duration_text(seconds)}</b>" if seconds else ""
        await update.effective_message.reply_text(
            f"🔇 {mention(target)} muted{suffix}.", parse_mode="HTML"
        )
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Telegram rejected the mute: {escape(str(exc))}", parse_mode="HTML")


async def unmute(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member.")
        return
    try:
        await perform_unmute(update.effective_chat, target.id)
        add_log(update.effective_chat.id, update.effective_user.id, "unmute", target.id)
        await update.effective_message.reply_text(f"🔊 {mention(target)} can speak again.", parse_mode="HTML")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Telegram rejected the action: {escape(str(exc))}", parse_mode="HTML")


async def ban(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member.")
        return
    ok, reason = await can_act_on_target(update.effective_chat, update.effective_user.id, target.id)
    if not ok:
        await update.effective_message.reply_text(f"⛔ {reason}")
        return
    try:
        await update.effective_chat.ban_member(target.id)
        increment_stat(update.effective_chat.id, "bans")
        add_log(update.effective_chat.id, update.effective_user.id, "ban", target.id)
        await update.effective_message.reply_text(f"🚫 {mention(target)} has been banned.", parse_mode="HTML")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Telegram rejected the ban: {escape(str(exc))}", parse_mode="HTML")


async def unban(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to a member's message to unban them.")
        return
    try:
        await update.effective_chat.unban_member(target.id, only_if_banned=True)
        add_log(update.effective_chat.id, update.effective_user.id, "unban", target.id)
        await update.effective_message.reply_text(f"✅ {mention(target)} has been unbanned.", parse_mode="HTML")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Telegram rejected the action: {escape(str(exc))}", parse_mode="HTML")


async def kick(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member.")
        return
    ok, reason = await can_act_on_target(update.effective_chat, update.effective_user.id, target.id)
    if not ok:
        await update.effective_message.reply_text(f"⛔ {reason}")
        return
    try:
        await update.effective_chat.ban_member(target.id)
        await update.effective_chat.unban_member(target.id, only_if_banned=True)
        add_log(update.effective_chat.id, update.effective_user.id, "kick", target.id)
        await update.effective_message.reply_text(f"👤 {mention(target)} was removed from the group.", parse_mode="HTML")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Telegram rejected the action: {escape(str(exc))}", parse_mode="HTML")


async def promote(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member you want to promote.")
        return
    if target.id == update.effective_user.id:
        await update.effective_message.reply_text("⛔ You cannot promote yourself.")
        return
    try:
        actor = await update.effective_chat.get_member(update.effective_user.id)
        if not getattr(actor, "can_promote_members", False) and actor.status != ChatMemberStatus.OWNER:
            await update.effective_message.reply_text("⛔ You do not have permission to promote members.")
            return
        await update.effective_chat.promote_member(
            target.id, can_manage_chat=False, can_delete_messages=True,
            can_manage_video_chats=False, can_restrict_members=True,
            can_promote_members=False, can_change_info=False, can_invite_users=True,
            can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
            can_manage_topics=False,
        )
        add_log(update.effective_chat.id, update.effective_user.id, "promote", target.id)
        await update.effective_message.reply_text(f"👮 {mention(target)} has been promoted to admin.", parse_mode="HTML")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Telegram rejected the promotion: {escape(str(exc))}", parse_mode="HTML")


async def demote(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the admin you want to demote.")
        return
    try:
        actor = await update.effective_chat.get_member(update.effective_user.id)
        if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_promote_members", False):
            await update.effective_message.reply_text("⛔ You do not have permission to demote admins.")
            return
        await update.effective_chat.promote_member(
            target.id, can_manage_chat=False, can_delete_messages=False,
            can_manage_video_chats=False, can_restrict_members=False,
            can_promote_members=False, can_change_info=False, can_invite_users=False,
            can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
            can_manage_topics=False,
        )
        add_log(update.effective_chat.id, update.effective_user.id, "demote", target.id)
        await update.effective_message.reply_text(f"👤 {mention(target)} is no longer an admin.", parse_mode="HTML")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Telegram rejected the demotion: {escape(str(exc))}", parse_mode="HTML")


async def delete_message(update, context):
    if not await require_admin(update): return
    if not update.effective_message.reply_to_message:
        await update.effective_message.reply_text("Reply to the message you want to delete.")
        return
    try:
        await update.effective_message.reply_to_message.delete()
        await update.effective_message.delete()
        add_log(update.effective_chat.id, update.effective_user.id, "delete")
        increment_stat(update.effective_chat.id, "deleted")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ Could not delete: {escape(str(exc))}", parse_mode="HTML")


async def purge(update, context):
    if not await require_admin(update): return
    if not update.effective_message.reply_to_message:
        await update.effective_message.reply_text("Reply to the oldest message to start a purge, then use /purge N.")
        return
    try:
        count = min(max(int(context.args[0]) if context.args else 10, 1), 100)
    except ValueError:
        await update.effective_message.reply_text("Use /purge followed by a number from 1 to 100.")
        return
    start_id = update.effective_message.reply_to_message.message_id
    ids = list(range(start_id, start_id + count))
    ids.append(update.effective_message.message_id)
    deleted = 0
    for i in range(0, len(ids), 100):
        try:
            await update.effective_chat.delete_messages(ids[i:i+100])
            deleted += len(ids[i:i+100])
        except TelegramError:
            for mid in ids[i:i+100]:
                try:
                    await update.effective_chat.delete_message(mid)
                    deleted += 1
                except TelegramError:
                    pass
    increment_stat(update.effective_chat.id, "deleted", deleted)
    add_log(update.effective_chat.id, update.effective_user.id, "purge", details=f"count={deleted}")


async def pin(update, context):
    if not await require_admin(update): return
    r = update.effective_message.reply_to_message
    if not r:
        await update.effective_message.reply_text("Reply to the message you want to pin.")
        return
    try:
        await r.pin(disable_notification=True)
        add_log(update.effective_chat.id, update.effective_user.id, "pin", details=str(r.message_id))
        await update.effective_message.reply_text("📌 Message pinned.")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ {escape(str(exc))}", parse_mode="HTML")


async def unpin(update, context):
    if not await require_admin(update): return
    try:
        await update.effective_chat.unpin_message()
        add_log(update.effective_chat.id, update.effective_user.id, "unpin")
        await update.effective_message.reply_text("📌 Message unpinned.")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ {escape(str(exc))}", parse_mode="HTML")


# -----------------------------
# Rules / group info / admins / stats
# -----------------------------

async def rules(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("Rules are available inside a group.")
        return
    text = get_rules(update.effective_chat.id)
    if not text:
        await update.effective_message.reply_text("📜 No group rules have been configured yet.")
        return
    await update.effective_message.reply_text(f"📜 <b>GROUP RULES</b>\n\n{escape(text)}", parse_mode="HTML")


async def setrules(update, context):
    if not await require_admin(update): return
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        await update.effective_message.reply_text("Usage: /setrules your rules text")
        return
    set_rules(update.effective_chat.id, text)
    add_log(update.effective_chat.id, update.effective_user.id, "setrules")
    await update.effective_message.reply_text("✅ Group rules updated.")


async def delrules(update, context):
    if not await require_admin(update): return
    set_rules(update.effective_chat.id, "")
    await update.effective_message.reply_text("✅ Group rules removed.")


async def info(update, context):
    chat = update.effective_chat
    if not is_group(update):
        await update.effective_message.reply_text("This command works in groups.")
        return
    count = getattr(chat, "members_count", None)
    try:
        count = await context.bot.get_chat_member_count(chat.id)
    except TelegramError:
        pass
    await update.effective_message.reply_text(
        f"<b>📊 GROUP INFO</b>\n\n"
        f"Name: <b>{escape(chat.title or 'Group')}</b>\n"
        f"ID: <code>{chat.id}</code>\n"
        f"Members: <b>{count if count is not None else 'Unknown'}</b>",
        parse_mode="HTML",
    )


async def admins(update, context):
    if not is_group(update): return
    try:
        members = await update.effective_chat.get_administrators()
        lines = ["<b>👮 GROUP ADMINS</b>", ""]
        for m in members:
            lines.append(f"• {mention(m.user)}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ {escape(str(exc))}", parse_mode="HTML")


async def stats(update, context):
    if not is_group(update): return
    ensure_chat(update.effective_chat.id)
    with DB_LOCK, db() as con:
        row = con.execute("SELECT * FROM stats WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
    await update.effective_message.reply_text(
        "<b>📊 NFC STATISTICS</b>\n\n"
        f"💬 Messages: <b>{row['messages']}</b>\n"
        f"🧹 Deleted: <b>{row['deleted']}</b>\n"
        f"👋 Joins: <b>{row['joins']}</b>\n"
        f"🚪 Leaves: <b>{row['leaves']}</b>\n"
        f"⚠️ Warnings: <b>{row['warnings']}</b>\n"
        f"🔇 Mutes: <b>{row['mutes']}</b>\n"
        f"🚫 Bans: <b>{row['bans']}</b>",
        parse_mode="HTML",
    )


# -----------------------------
# Welcome / goodbye
# -----------------------------

async def welcome_command(update, context):
    if not await require_admin(update): return
    if context.args and context.args[0].lower() in ("on", "off"):
        enabled = context.args[0].lower() == "on"
        set_setting(update.effective_chat.id, "welcome", enabled)
        await update.effective_message.reply_text(f"👋 Welcome messages: {'ON' if enabled else 'OFF'}")
        return
    await update.effective_message.reply_text(f"👋 Welcome messages are {'ON' if get_setting(update.effective_chat.id,'welcome') else 'OFF'}. Use /welcome on|off")


async def goodbye_command(update, context):
    if not await require_admin(update): return
    if context.args and context.args[0].lower() in ("on", "off"):
        enabled = context.args[0].lower() == "on"
        set_setting(update.effective_chat.id, "goodbye", enabled)
        await update.effective_message.reply_text(f"🚪 Goodbye messages: {'ON' if enabled else 'OFF'}")
        return
    await update.effective_message.reply_text(f"🚪 Goodbye messages are {'ON' if get_setting(update.effective_chat.id,'goodbye') else 'OFF'}. Use /goodbye on|off")


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm or not cm.chat:
        return
    chat = cm.chat
    ensure_chat(chat.id, chat.title or "")
    old = cm.old_chat_member.status
    new = cm.new_chat_member.status
    user = cm.new_chat_member.user
    if old in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        increment_stat(chat.id, "joins")
        if get_setting(chat.id, "welcome") and not user.is_bot:
            try:
                await context.bot.send_message(chat.id, f"👋 Welcome {mention(user)}!", parse_mode="HTML")
            except TelegramError:
                pass
    elif new in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and old not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        increment_stat(chat.id, "leaves")
        if get_setting(chat.id, "goodbye") and not user.is_bot:
            try:
                await context.bot.send_message(chat.id, f"🚪 {mention(user)} left the group.", parse_mode="HTML")
            except TelegramError:
                pass


# -----------------------------
# Filters / locks
# -----------------------------

LOCK_TYPES = {
    "links": "Links",
    "photos": "Photos",
    "videos": "Videos",
    "gifs": "GIFs",
    "stickers": "Stickers",
    "documents": "Documents",
    "audio": "Audio",
    "voice": "Voice",
    "polls": "Polls",
    "forwards": "Forwards",
    "commands": "Commands",
}


def get_locks(chat_id):
    with DB_LOCK, db() as con:
        rows = con.execute("SELECT content_type FROM locks WHERE chat_id=? AND enabled=1", (chat_id,)).fetchall()
    return {r[0] for r in rows}


async def locks_command(update, context):
    if not await require_admin(update): return
    chat_id = update.effective_chat.id
    if not context.args:
        active = get_locks(chat_id)
        lines = ["<b>🔒 CONTENT LOCKS</b>", "", "Use /lock type or /unlock type", ""]
        for k, label in LOCK_TYPES.items():
            lines.append(f"{'🔒' if k in active else '🔓'} {label}: {'ON' if k in active else 'OFF'}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    await update.effective_message.reply_text("Use /lock <type> or /unlock <type>.")


async def lock_command(update, context):
    if not await require_admin(update): return
    if not context.args or context.args[0].lower() not in LOCK_TYPES:
        await update.effective_message.reply_text("Types: " + ", ".join(LOCK_TYPES))
        return
    key = context.args[0].lower()
    with DB_LOCK, db() as con:
        con.execute("INSERT INTO locks(chat_id,content_type,enabled) VALUES(?,?,1) ON CONFLICT(chat_id,content_type) DO UPDATE SET enabled=1", (update.effective_chat.id, key))
    set_setting(update.effective_chat.id, "locks", True)
    await update.effective_message.reply_text(f"🔒 {LOCK_TYPES[key]} locked.")


async def unlock_command(update, context):
    if not await require_admin(update): return
    if not context.args or context.args[0].lower() not in LOCK_TYPES:
        await update.effective_message.reply_text("Types: " + ", ".join(LOCK_TYPES))
        return
    key = context.args[0].lower()
    with DB_LOCK, db() as con:
        con.execute("UPDATE locks SET enabled=0 WHERE chat_id=? AND content_type=?", (update.effective_chat.id, key))
    await update.effective_message.reply_text(f"🔓 {LOCK_TYPES[key]} unlocked.")


async def filter_command(update, context):
    if not await require_admin(update): return
    raw = update.effective_message.text.partition(" ")[2].strip()
    parts = raw.split("|", 2)
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /filter trigger | action | optional response\nActions: delete, warn, mute, ban")
        return
    trigger = parts[0].strip().lower()
    action = parts[1].strip().lower()
    response = parts[2].strip() if len(parts) > 2 else ""
    if not trigger or action not in {"delete", "warn", "mute", "ban"}:
        await update.effective_message.reply_text("Invalid filter. Actions: delete, warn, mute, ban")
        return
    with DB_LOCK, db() as con:
        con.execute("INSERT INTO filters(chat_id,trigger,response,action) VALUES(?,?,?,?) ON CONFLICT(chat_id,trigger) DO UPDATE SET response=excluded.response,action=excluded.action", (update.effective_chat.id, trigger, response, action))
    await update.effective_message.reply_text(f"✅ Filter added for <code>{escape(trigger)}</code>.", parse_mode="HTML")


async def stop_filter(update, context):
    if not await require_admin(update): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /stop trigger")
        return
    trigger = " ".join(context.args).strip().lower()
    with DB_LOCK, db() as con:
        con.execute("DELETE FROM filters WHERE chat_id=? AND trigger=?", (update.effective_chat.id, trigger))
    await update.effective_message.reply_text("✅ Filter removed.")


async def filters_command(update, context):
    if not await require_admin(update): return
    with DB_LOCK, db() as con:
        rows = con.execute("SELECT trigger,action FROM filters WHERE chat_id=? ORDER BY trigger", (update.effective_chat.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("📝 No filters configured.")
        return
    text = "<b>📝 FILTERS</b>\n\n" + "\n".join(f"• <code>{escape(r['trigger'])}</code> → {escape(r['action'])}" for r in rows)
    await update.effective_message.reply_text(text, parse_mode="HTML")


# -----------------------------
# Approved / whitelist members
# -----------------------------

async def approve(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member you want to approve.")
        return
    with DB_LOCK, db() as con:
        con.execute("INSERT OR REPLACE INTO approved(chat_id,user_id,added_by,created_at) VALUES(?,?,?,?)", (update.effective_chat.id, target.id, update.effective_user.id, int(datetime.now(timezone.utc).timestamp())))
    await update.effective_message.reply_text(f"✅ {mention(target)} is now approved and exempt from NFC content protection.", parse_mode="HTML")


async def unapprove(update, context):
    if not await require_admin(update): return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the member you want to unapprove.")
        return
    with DB_LOCK, db() as con:
        con.execute("DELETE FROM approved WHERE chat_id=? AND user_id=?", (update.effective_chat.id, target.id))
    await update.effective_message.reply_text(f"✅ Approval removed for {mention(target)}.", parse_mode="HTML")


async def approved(update, context):
    if not await require_admin(update): return
    with DB_LOCK, db() as con:
        rows = con.execute("SELECT user_id FROM approved WHERE chat_id=? ORDER BY created_at", (update.effective_chat.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("No approved members.")
        return
    lines = ["<b>✅ APPROVED MEMBERS</b>", ""]
    for row in rows:
        try:
            u = await context.bot.get_chat(row[0])
            lines.append(f"• {mention(u)} — <code>{u.id}</code>")
        except TelegramError:
            lines.append(f"• <code>{row[0]}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# -----------------------------
# AFK / notes / reports
# -----------------------------

async def afk(update, context):
    reason = " ".join(context.args).strip() if context.args else "AFK"
    now = int(datetime.now(timezone.utc).timestamp())
    with DB_LOCK, db() as con:
        con.execute("INSERT INTO afk(user_id,reason,since) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason,since=excluded.since", (update.effective_user.id, reason, now))
    await update.effective_message.reply_text(f"💤 {mention(update.effective_user)} is now AFK.\nReason: {escape(reason)}", parse_mode="HTML")



async def note(update, context):
    if not await require_admin(update): return
    raw = update.effective_message.text.partition(" ")[2].strip()
    if "|" not in raw:
        await update.effective_message.reply_text("Usage: /note name | text")
        return
    name, value = [x.strip() for x in raw.split("|", 1)]
    if not name or not value:
        await update.effective_message.reply_text("Both note name and text are required.")
        return
    with DB_LOCK, db() as con:
        con.execute("INSERT INTO notes(chat_id,name,value) VALUES(?,?,?) ON CONFLICT(chat_id,name) DO UPDATE SET value=excluded.value", (update.effective_chat.id, name.lower(), value))
    await update.effective_message.reply_text(f"✅ Note <code>{escape(name.lower())}</code> saved.", parse_mode="HTML")


async def getnote(update, context):
    if not is_group(update) or not context.args:
        await update.effective_message.reply_text("Usage: /getnote name")
        return
    name = " ".join(context.args).lower()
    with DB_LOCK, db() as con:
        row = con.execute("SELECT value FROM notes WHERE chat_id=? AND name=?", (update.effective_chat.id, name)).fetchone()
    if not row:
        await update.effective_message.reply_text("No note with that name.")
        return
    await update.effective_message.reply_text(f"📌 <b>{escape(name)}</b>\n\n{escape(row[0])}", parse_mode="HTML")


async def report(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("Report is available in groups.")
        return
    target = target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the message you want to report, then use /report [reason].")
        return
    reason = " ".join(context.args).strip() or "No reason provided"
    admins = await update.effective_chat.get_administrators()
    text = (
        "🚨 <b>MEMBER REPORT</b>\n\n"
        f"Reporter: {mention(update.effective_user)}\n"
        f"Reported: {mention(target)}\n"
        f"Reason: {escape(reason)}"
    )
    sent = 0
    for admin in admins:
        if admin.user.is_bot:
            continue
        try:
            await context.bot.send_message(admin.user.id, text, parse_mode="HTML")
            sent += 1
        except TelegramError:
            pass
    await update.effective_message.reply_text(f"🚨 Report sent to {sent} reachable admin(s).")


# -----------------------------
# User info / ID
# -----------------------------

async def userinfo(update, context):
    target = target_from_reply(update) or update.effective_user
    text = (
        "<b>👤 USER INFO</b>\n\n"
        f"Name: {mention(target)}\n"
        f"ID: <code>{target.id}</code>\n"
        f"Username: @{escape(target.username) if target.username else 'none'}"
    )
    if is_group(update):
        try:
            m = await update.effective_chat.get_member(target.id)
            text += f"\nStatus: <b>{escape(m.status)}</b>"
        except TelegramError:
            pass
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def id_command(update, context):
    target = target_from_reply(update) or update.effective_user
    await update.effective_message.reply_text(f"🆔 User ID: <code>{target.id}</code>", parse_mode="HTML")


# -----------------------------
# Message protection
# -----------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ensure_chat(chat.id, chat.title or "")
    if msg.from_user:
        if msg.from_user.is_bot:
            if get_setting(chat.id, "antibot"):
                try:
                    await msg.delete()
                    increment_stat(chat.id, "deleted")
                except TelegramError:
                    pass
            return
        # Remove AFK status when the person talks.
        with DB_LOCK, db() as con:
            afk_row = con.execute("SELECT since FROM afk WHERE user_id=?", (msg.from_user.id,)).fetchone()
            if afk_row:
                con.execute("DELETE FROM afk WHERE user_id=?", (msg.from_user.id,))
        if afk_row:
            elapsed = max(0, int(datetime.now(timezone.utc).timestamp()) - int(afk_row[0]))
            try:
                await msg.reply_text(f"👋 Welcome back, {mention(msg.from_user)}. AFK time: <b>{duration_text(elapsed)}</b>", parse_mode="HTML")
            except TelegramError:
                pass
    increment_stat(chat.id, "messages")

    # AFK mentions / replies.
    target_users = []
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_users.append(msg.reply_to_message.from_user.id)
    if msg.entities:
        for ent in msg.entities:
            if ent.type == "text_mention" and ent.user:
                target_users.append(ent.user.id)
    for uid in set(target_users):
        with DB_LOCK, db() as con:
            row = con.execute("SELECT reason FROM afk WHERE user_id=?", (uid,)).fetchone()
        if row:
            try:
                await msg.reply_text(f"💤 <a href=\"tg://user?id={uid}\">User</a> is AFK.\nReason: {escape(row[0])}", parse_mode="HTML")
            except TelegramError:
                pass

    # Admins and explicitly approved members bypass content protection.
    if msg.from_user and await is_admin(chat, msg.from_user.id):
        return
    if msg.from_user:
        with DB_LOCK, db() as con:
            approved_row = con.execute("SELECT 1 FROM approved WHERE chat_id=? AND user_id=?", (chat.id, msg.from_user.id)).fetchone()
        if approved_row:
            return

    text = msg.text or msg.caption or ""

    # Anti-channel: remove messages sent on behalf of channels.
    if get_setting(chat.id, "automod") and get_setting(chat.id, "anti_channel") and msg.sender_chat:
        try:
            await msg.delete(); increment_stat(chat.id, "deleted"); return
        except TelegramError:
            pass

    # Anti-service: remove join/leave/pin service messages when enabled.
    if get_setting(chat.id, "automod") and get_setting(chat.id, "anti_service"):
        service = bool(msg.new_chat_members or msg.left_chat_member or msg.new_chat_title or msg.pinned_message or msg.new_chat_photo or msg.delete_chat_photo)
        if service:
            try:
                await msg.delete(); increment_stat(chat.id, "deleted"); return
            except TelegramError:
                pass

    # Anti-forward.
    if get_setting(chat.id, "automod") and get_setting(chat.id, "antiforward") and (msg.forward_origin or getattr(msg, "forward_from", None)):
        try:
            await msg.delete()
            increment_stat(chat.id, "deleted")
            return
        except TelegramError:
            pass

    # Link filter.
    if get_setting(chat.id, "automod") and get_setting(chat.id, "antilink") and URL_RE.search(text):
        try:
            await msg.delete()
            increment_stat(chat.id, "deleted")
            return
        except TelegramError:
            pass

    # Mention spam.
    if get_setting(chat.id, "automod") and get_setting(chat.id, "antimention"):
        mentions = len(MENTION_RE.findall(text))
        if mentions >= 5:
            try:
                await msg.delete()
                increment_stat(chat.id, "deleted")
                return
            except TelegramError:
                pass

    # Content locks.
    if get_setting(chat.id, "locks"):
        locks = get_locks(chat.id)
        content_map = {
            "photos": bool(msg.photo),
            "videos": bool(msg.video or msg.video_note),
            "gifs": bool(msg.animation),
            "stickers": bool(msg.sticker),
            "documents": bool(msg.document),
            "audio": bool(msg.audio),
            "voice": bool(msg.voice),
            "polls": bool(msg.poll),
            "forwards": bool(msg.forward_origin or getattr(msg, "forward_from", None)),
            "commands": bool(msg.text and msg.text.startswith("/")),
        }
        for key, present in content_map.items():
            if key in locks and present:
                try:
                    await msg.delete()
                    increment_stat(chat.id, "deleted")
                    return
                except TelegramError:
                    break

    # Custom filters.
    if get_setting(chat.id, "filters") and text:
        lower = text.lower()
        with DB_LOCK, db() as con:
            rows = con.execute("SELECT trigger,response,action FROM filters WHERE chat_id=?", (chat.id,)).fetchall()
        for row in rows:
            if row["trigger"].lower() in lower:
                action = row["action"]
                try:
                    if action == "delete":
                        await msg.delete()
                        increment_stat(chat.id, "deleted")
                    elif action == "warn" and msg.from_user:
                        count = get_warnings(chat.id, msg.from_user.id) + 1
                        set_warnings(chat.id, msg.from_user.id, count)
                        increment_stat(chat.id, "warnings")
                        await msg.reply_text(f"⚠️ {mention(msg.from_user)} received a warning. ({count})", parse_mode="HTML")
                    elif action == "mute" and msg.from_user:
                        await perform_mute(chat, msg.from_user.id, 3600)
                        increment_stat(chat.id, "mutes")
                    elif action == "ban" and msg.from_user:
                        await chat.ban_member(msg.from_user.id)
                        increment_stat(chat.id, "bans")
                    if row["response"]:
                        await context.bot.send_message(chat.id, row["response"])
                except TelegramError:
                    pass
                return

    # Repeat-message protection.
    if get_setting(chat.id, "automod") and get_setting(chat.id, "repeat_guard") and msg.from_user and text:
        key = f"{chat.id}:{msg.from_user.id}"
        if not hasattr(handle_message, "_last_text"):
            handle_message._last_text = {}
        previous = handle_message._last_text.get(key)
        if previous == text.strip().lower():
            try:
                await msg.delete()
                increment_stat(chat.id, "deleted")
                return
            except TelegramError:
                pass
        handle_message._last_text[key] = text.strip().lower()

    # Flood protection: 6 messages in 5 seconds.
    if get_setting(chat.id, "automod") and get_setting(chat.id, "antiflood") and msg.from_user:
        now = int(datetime.now(timezone.utc).timestamp())
        with DB_LOCK, db() as con:
            con.execute("DELETE FROM flood WHERE chat_id=? AND ts<?", (chat.id, now - 5))
            con.execute("INSERT INTO flood(chat_id,user_id,ts) VALUES(?,?,?)", (chat.id, msg.from_user.id, now))
            row = con.execute("SELECT COUNT(*) FROM flood WHERE chat_id=? AND user_id=?", (chat.id, msg.from_user.id)).fetchone()
        if row[0] >= 6:
            try:
                await perform_mute(chat, msg.from_user.id, 60)
                increment_stat(chat.id, "mutes")
                with DB_LOCK, db() as con:
                    con.execute("DELETE FROM flood WHERE chat_id=? AND user_id=?", (chat.id, msg.from_user.id))
            except TelegramError:
                pass


# -----------------------------
# Simple toggles
# -----------------------------

async def toggle_command(update, context):
    if not await require_admin(update): return
    command = update.effective_message.text.split()[0].split("@")[0].lstrip("/").lower()
    mapping = {
        "antispam": "antispam", "antilink": "antilink", "antiflood": "antiflood",
        "antiforward": "antiforward", "antibot": "antibot", "antimention": "antimention",
        "antichannel": "anti_channel", "antiservice": "anti_service", "repeatguard": "repeat_guard",
    }
    key = mapping.get(command)
    if not key:
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text(f"Use /{command} on or /{command} off")
        return
    enabled = context.args[0].lower() == "on"
    set_setting(update.effective_chat.id, key, enabled)
    set_setting(update.effective_chat.id, "automod", True)
    await update.effective_message.reply_text(f"{SETTING_LABELS[key]}: {'ON' if enabled else 'OFF'}")


async def fileid_command(update, context):
    """Return a Telegram file_id for a replied-to media message.
    Useful for setting INTRO_GIF without hard-coding a URL.
    """
    user = update.effective_user
    allowed = bool(ADMIN_ID.isdigit() and user and user.id == int(ADMIN_ID))
    if not allowed:
        await update.effective_message.reply_text("⛔ This utility is owner-only.")
        return
    r = update.effective_message.reply_to_message
    if not r:
        await update.effective_message.reply_text("Reply to a GIF/animation and use /fileid.")
        return
    file_id = None
    if r.animation:
        file_id = r.animation.file_id
    elif r.document and (r.document.mime_type or "").startswith("video/"):
        file_id = r.document.file_id
    if not file_id:
        await update.effective_message.reply_text("That message does not contain an animation/GIF.")
        return
    await update.effective_message.reply_text(f"<code>{escape(file_id)}</code>", parse_mode="HTML")


# -----------------------------
# Error handling / commands
# -----------------------------

async def error_handler(update, context):
    log.error("Update error: %s", context.error, exc_info=context.error)


async def post_init(application: Application):
    commands = [
        ("start", "Open NFC"),
        ("help", "Management help"),
        ("settings", "Group settings"),
        ("warn", "Warn a member"),
        ("warnings", "View warnings"),
        ("resetwarn", "Reset warnings"),
        ("mute", "Mute a member"),
        ("unmute", "Unmute a member"),
        ("ban", "Ban a member"),
        ("unban", "Unban a member"),
        ("kick", "Remove a member"),
        ("promote", "Promote a member"),
        ("demote", "Demote an admin"),
        ("del", "Delete a message"),
        ("purge", "Delete messages"),
        ("pin", "Pin a message"),
        ("unpin", "Unpin a message"),
        ("rules", "View group rules"),
        ("setrules", "Set group rules"),
        ("delrules", "Delete group rules"),
        ("info", "Group information"),
        ("admins", "List admins"),
        ("stats", "Group statistics"),
        ("welcome", "Welcome settings"),
        ("goodbye", "Goodbye settings"),
        ("locks", "Content locks"),
        ("lock", "Lock content type"),
        ("unlock", "Unlock content type"),
        ("filter", "Create a filter"),
        ("filters", "List filters"),
        ("stop", "Remove a filter"),
        ("approve", "Approve a member"),
        ("unapprove", "Remove approval"),
        ("approved", "List approved members"),
        ("afk", "Set AFK status"),
            ("report", "Report a message"),
        ("userinfo", "User information"),
        ("id", "Show user ID"),
        ("note", "Save an admin note"),
        ("getnote", "Read an admin note"),
        ("fileid", "Get a media file ID"),
        ("antispam", "Toggle anti-spam"),
        ("antilink", "Toggle anti-link"),
        ("antiflood", "Toggle anti-flood"),
        ("antiforward", "Toggle anti-forward"),
        ("antibot", "Toggle anti-bot"),
        ("antimention", "Toggle anti-mention"),
        ("antichannel", "Toggle anti-channel"),
        ("antiservice", "Toggle anti-service"),
        ("repeatguard", "Toggle repeat guard"),
    ]
    await application.bot.set_my_commands([BotCommand(c, d) for c, d in commands])


# -----------------------------
# Main
# -----------------------------


def build_application():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    init_db()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Core
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^(set:|settings:)"))

    # Moderation
    application.add_handler(CommandHandler("warn", warn))
    application.add_handler(CommandHandler("warnings", warnings))
    application.add_handler(CommandHandler("resetwarn", resetwarn))
    application.add_handler(CommandHandler("mute", mute))
    application.add_handler(CommandHandler("unmute", unmute))
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("unban", unban))
    application.add_handler(CommandHandler("kick", kick))
    application.add_handler(CommandHandler("promote", promote))
    application.add_handler(CommandHandler("demote", demote))
    application.add_handler(CommandHandler("del", delete_message))
    application.add_handler(CommandHandler("purge", purge))
    application.add_handler(CommandHandler("pin", pin))
    application.add_handler(CommandHandler("unpin", unpin))

    # Group
    application.add_handler(CommandHandler("rules", rules))
    application.add_handler(CommandHandler("setrules", setrules))
    application.add_handler(CommandHandler("delrules", delrules))
    application.add_handler(CommandHandler("info", info))
    application.add_handler(CommandHandler("admins", admins))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("welcome", welcome_command))
    application.add_handler(CommandHandler("goodbye", goodbye_command))

    # Protection
    application.add_handler(CommandHandler("locks", locks_command))
    application.add_handler(CommandHandler("lock", lock_command))
    application.add_handler(CommandHandler("unlock", unlock_command))
    application.add_handler(CommandHandler("filter", filter_command))
    application.add_handler(CommandHandler("filters", filters_command))
    application.add_handler(CommandHandler("stop", stop_filter))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("unapprove", unapprove))
    application.add_handler(CommandHandler("approved", approved))
    application.add_handler(CommandHandler("antispam", toggle_command))
    application.add_handler(CommandHandler("antilink", toggle_command))
    application.add_handler(CommandHandler("antiflood", toggle_command))
    application.add_handler(CommandHandler("antiforward", toggle_command))
    application.add_handler(CommandHandler("antibot", toggle_command))
    application.add_handler(CommandHandler("antimention", toggle_command))
    application.add_handler(CommandHandler("antichannel", toggle_command))
    application.add_handler(CommandHandler("antiservice", toggle_command))
    application.add_handler(CommandHandler("repeatguard", toggle_command))

    # Utilities
    application.add_handler(CommandHandler("afk", afk))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("userinfo", userinfo))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("note", note))
    application.add_handler(CommandHandler("getnote", getnote))
    application.add_handler(CommandHandler("fileid", fileid_command))

    application.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    return application


if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    bot_app = build_application()
    log.info("NFC starting")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
