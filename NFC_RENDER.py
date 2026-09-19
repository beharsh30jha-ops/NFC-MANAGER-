import os
import re
import json
import random
import logging
import time
from datetime import datetime, date

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from html import escape
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import TelegramError

# ============================================================
# NFC / NFCP — Group utility + reactions bot
# Local testing version for Pydroid 3
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

from flask import Flask
from threading import Thread

DATA_FILE = "nfc_data.json"
STICKER_CACHE_FILE = "nfc_sticker_cache.json"

# Your supplied image URL is kept here for later use.
# For teen-safe group use, /duo uses a neutral team-up card instead
# of a romantic pairing image.
COUPLE_IMAGE_URL = "https://clck.ru/3VuTqX"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)

# ------------------------------------------------------------
# Sticker packs supplied by the user
# ------------------------------------------------------------

STICKER_PACKS = {
    "love": "Z07jy_by_CalsiBot",
    "hug": "Z07jy_by_CalsiBot",
    "kiss": "sticker_8ba9c63f_by_moe_sticker_bot",
    "miss": "hamsterset",
    "care": "vid_7158329525_by_Nobara_Xprobot",
    "crush": "kang_5852054126_by_Sticker_kang_robot",
    "blush": "airaalol",
    "shy": "new_ssssssssssssssss_by_fStikBot",
    "laugh": "Demon1516",
    "funny": "sp3020a32d5ac3791e33f14eac3207f8f2_by_stckrRobot",
    "troll": "BestPhotosOAT",
    "wtf": "Brain_Rot708",
    "wow": "set_ravibhaiya_by_TgEmojiBot",
    "bruh": "LEO_ca963_by_TgEmojis_bot",
    "aura": "Arykr",
    "swag": "pro_23_3",
    "dance": "whocares45_x_ll_NICK_ll_DANCE_VOL_1_by_fStikBot",
    "happy": "moons_fav",
    "sad": "MOL5_by_fStikBot",
    "cry": "Quby741",
    "mad": "MegaEditobusi_by_fStikBot",
    "scared": "tinycatss",
    "excited": "CRASHOVERRIDE",
    "welcome": "Ishahahagaha_by_fStikBot",
    "bye": "Sakshi85",
    "sorry": "stik_3_49992_by_TgEmojis_bot",
    "slap": "Grand_Flea_by_fStikBot",
    "kick": "Grand_Flea_by_fStikBot",
    "remove": "Grand_Flea_by_fStikBot",
}

# ------------------------------------------------------------
# Persistence
# ------------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

data = load_json(DATA_FILE, {"chats": {}, "users": {}})
sticker_cache = load_json(STICKER_CACHE_FILE, {})

def get_chat(chat_id):
    cid = str(chat_id)
    data["chats"].setdefault(cid, {
        "enabled": True,
        "open": True,
        "whitelist": [],
        "economy": {},
        "daily": {},
        "coupons": [],
        "powers": {},
    })
    return data["chats"][cid]

def remember_user(user):
    if not user or user.is_bot:
        return
    uid = str(user.id)
    data["users"].setdefault(uid, {
        "id": user.id,
        "name": user.full_name or "User",
        "username": user.username or "",
        "last_seen": int(time.time()),
    })
    data["users"][uid]["name"] = user.full_name or data["users"][uid]["name"]
    data["users"][uid]["username"] = user.username or data["users"][uid]["username"]
    data["users"][uid]["last_seen"] = int(time.time())

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

URL_PATTERN = re.compile(
    r"(?i)(?:https?://[^\s<>()]+|www\.[^\s<>()]+|"
    r"(?:[a-z0-9-]+\.)+(?:com|net|org|in|co|io|me|xyz|site|"
    r"online|app|dev|info|biz|ly|gg|shop|store)(?:/[^\s<>()]*)?)"
)

def has_url(text):
    return bool(text and URL_PATTERN.search(text))

def clean_name(user):
    return (user.full_name or "User").replace("\n", " ")

async def is_admin(update, user_id=None):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return False
    user_id = user_id or update.effective_user.id
    try:
        m = await chat.get_member(user_id)
        return m.status in ("administrator", "creator")
    except TelegramError:
        return False

async def require_admin(update):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("This command works in groups only.")
        return False
    if not await is_admin(update):
        await update.effective_message.reply_text("Only group admins can use this command.")
        return False
    return True

def reply_target(update):
    r = update.effective_message.reply_to_message
    return r.from_user if r and r.from_user else None

def user_line(user, emoji="👤"):
    """Clickable Telegram mention with a custom display name + emoji."""
    return f'<a href="tg://user?id={user.id}">{escape(emoji + " " + clean_name(user))}</a>'

def command_link(command):
    """Return a plain Telegram bot command.

    Do NOT wrap commands in Markdown code or text links: Telegram then
    automatically creates a bot_command entity, making the command blue
    and clickable/tappable in supported clients.
    """
    return command if command.startswith("/") else "/" + command.lstrip("/")

def actor_target_line(actor, target, emoji="✨", action="interacted with"):
    return (f"{user_line(actor, emoji)} <b>{escape(action)}</b> "
            f"{user_line(target, emoji)}")

# ------------------------------------------------------------
# Start / help
# ------------------------------------------------------------

async def start(update, context):
    me = await context.bot.get_me()
    keyboard = None
    if me.username:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add NFC to Group",
                                 url=f"https://t.me/{me.username}?startgroup=true")
        ]])

    text = (
        "🌸 *Welcome to NFC*\n\n"
        "A clean group utility bot for protection, reactions, "
        "games, profiles and group tools.\n\n"
        "Use `/help` to see the command menu."
    )
    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=keyboard
    )

async def help_command(update, context):
    def cmds(items):
        return "  ".join(command_link(x) for x in items)

    text = (
        "🌸 <b>NFC COMMAND MENU</b>\n\n"
        "🔹 <b>Basic</b>\n"
        f"{cmds(['/start','/help','/settings','/links'])}\n"
        f"{cmds(['/whitelist','/blacklist','/intro','/open','/close'])}\n"
        f"{cmds(['/admin','/admins','/own','/friends','/duo'])}\n"
        f"{cmds(['/kick','/remove'])}\n\n"
        "💬 <b>Social / Reply</b>\n"
        f"{cmds(['/hug','/care','/laugh','/funny','/wow'])}\n"
        f"{cmds(['/slap','/bonk','/poke','/brain','/stupid_meter'])}\n"
        f"{cmds(['/truth','/dare','/translate','/detail'])}\n\n"
        "💰 <b>Economy</b>\n"
        f"{cmds(['/economy','/gems','/convert','/daily'])}\n"
        f"{cmds(['/claim10k','/coupons','/bal','/stats'])}\n"
        f"{cmds(['/save','/give','/protect','/rank','/items','/gift'])}\n\n"
        "🎮 <b>Games</b>\n"
        f"{cmds(['/ludo','/puzzle','/minigames','/rps','/guess'])}\n\n"
        "🎭 <b>Sticker Reactions</b>\n"
        f"{cmds(['/love','/hug','/kiss','/miss','/care','/crush','/blush','/shy'])}\n"
        f"{cmds(['/laugh','/funny','/troll','/wtf','/wow','/bruh'])}\n"
        f"{cmds(['/aura','/swag','/dance','/happy','/sad','/cry'])}\n"
        f"{cmds(['/mad','/scared','/excited','/welcome','/bye','/sorry','/slap','/kick','/remove'])}\n\n"
        "✨ <i>Reply-based actions show both users as clickable names with an emoji.</i>"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

# ------------------------------------------------------------
# Protection
# ------------------------------------------------------------

async def settings(update, context):
    if not await require_admin(update):
        return
    c = get_chat(update.effective_chat.id)
    await update.effective_message.reply_text(
        "⚙️ *Group Settings*\n\n"
        f"Link protection: {'🟢 ON' if c['enabled'] else '🔴 OFF'}\n"
        f"Group mode: {'🟢 OPEN' if c['open'] else '🔴 CLOSED'}\n"
        f"Whitelist: `{len(c['whitelist'])}` users",
        parse_mode="Markdown"
    )

async def links(update, context):
    if not await require_admin(update):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Use `/links on` or `/links off`.", parse_mode="Markdown")
        return
    c = get_chat(update.effective_chat.id)
    c["enabled"] = context.args[0].lower() == "on"
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text(
        f"🔗 Link protection {'enabled' if c['enabled'] else 'disabled'}."
    )

async def whitelist(update, context):
    if not await require_admin(update):
        return
    c = get_chat(update.effective_chat.id)
    if context.args and context.args[0].lower() == "clear":
        c["whitelist"] = []
        save_json(DATA_FILE, data)
        await update.effective_message.reply_text("✓ Whitelist cleared.")
        return
    target = reply_target(update)
    if not target and context.args and context.args[0].isdigit():
        target_id = context.args[0]
    elif target:
        target_id = str(target.id)
    else:
        if not c["whitelist"]:
            await update.effective_message.reply_text("Whitelist is empty.")
        else:
            await update.effective_message.reply_text(
                "✓ Whitelist\n\n" + "\n".join(f"• `{x}`" for x in c["whitelist"]),
                parse_mode="Markdown"
            )
        return
    if target_id not in c["whitelist"]:
        c["whitelist"].append(target_id)
        save_json(DATA_FILE, data)
    await update.effective_message.reply_text("✓ User added to whitelist.")

async def blacklist(update, context):
    if not await require_admin(update):
        return
    c = get_chat(update.effective_chat.id)
    target = reply_target(update)
    target_id = str(target.id) if target else (context.args[0] if context.args and context.args[0].isdigit() else None)
    if not target_id:
        await update.effective_message.reply_text("Reply to a user or provide their numeric ID.")
        return
    if target_id in c["whitelist"]:
        c["whitelist"].remove(target_id)
        save_json(DATA_FILE, data)
        await update.effective_message.reply_text("✓ User removed from whitelist.")
    else:
        await update.effective_message.reply_text("That user is not whitelisted.")

async def open_group(update, context):
    if not await require_admin(update):
        return
    c = get_chat(update.effective_chat.id)
    c["open"] = True
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text("🟢 Group mode opened.")

async def close_group(update, context):
    if not await require_admin(update):
        return
    c = get_chat(update.effective_chat.id)
    c["open"] = False
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text("🔒 Group mode marked closed. (NFC does not remove members.)")

async def intro(update, context):
    await update.effective_message.reply_text(
        "🌸 *NFC*\n"
        "Group utilities • reactions • games • economy • protection\n\n"
        "Type `/help` for the complete menu.",
        parse_mode="Markdown"
    )

async def admin_info(update, context):
    if not await require_admin(update):
        return
    await update.effective_message.reply_text("🛡️ You have administrator access.")

async def admins(update, context):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    try:
        members = await chat.get_administrators()
        text = "🛡️ *Group Admins*\n\n" + "\n".join(f"• {user_line(m.user)}" for m in members)
        await update.effective_message.reply_text(text, parse_mode="Markdown")
    except TelegramError:
        await update.effective_message.reply_text("I couldn't read the admin list.")

async def own(update, context):
    await update.effective_message.reply_text("👑 The group owner can be viewed through Telegram's group info.")

# ------------------------------------------------------------
# Economy
# ------------------------------------------------------------

def wallet(uid):
    data["users"].setdefault(str(uid), {})
    u = data["users"][str(uid)]
    u.setdefault("coins", 0)
    u.setdefault("gems", 0)
    u.setdefault("daily", "")
    return u

async def bal(update, context):
    u = wallet(update.effective_user.id)
    await update.effective_message.reply_text(
        f"💰 *Balance*\n\nCoins: `{u['coins']}`\nGems: `{u['gems']}`",
        parse_mode="Markdown"
    )

async def economy(update, context):
    await bal(update, context)

async def gems(update, context):
    u = wallet(update.effective_user.id)
    await update.effective_message.reply_text(f"💎 Gems: `{u['gems']}`", parse_mode="Markdown")

async def daily(update, context):
    u = wallet(update.effective_user.id)
    today = str(date.today())
    if u.get("daily") == today:
        await update.effective_message.reply_text("⏳ Daily reward already claimed today.")
        return
    reward = random.randint(100, 300)
    u["coins"] += reward
    u["daily"] = today
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text(f"🎁 Daily reward: `{reward}` coins!", parse_mode="Markdown")

async def claim10k(update, context):
    u = wallet(update.effective_user.id)
    if u.get("claimed10k"):
        await update.effective_message.reply_text("You already claimed this starter reward.")
        return
    u["coins"] += 10000
    u["claimed10k"] = True
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text("🎁 Starter reward claimed: 10,000 coins.")

async def convert(update, context):
    u = wallet(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text("Use `/convert 100` to convert 100 coins into 1 gem.", parse_mode="Markdown")
        return
    try:
        coins = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Enter a whole number.")
        return
    if coins < 100 or u["coins"] < coins:
        await update.effective_message.reply_text("You need at least 100 coins and enough balance.")
        return
    gems_gained = coins // 100
    u["coins"] -= gems_gained * 100
    u["gems"] += gems_gained
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text(f"💎 Converted `{gems_gained * 100}` coins → `{gems_gained}` gems.", parse_mode="Markdown")

async def give(update, context):
    target = reply_target(update)
    if not target or not context.args:
        await update.effective_message.reply_text("Reply to a user and use `/give AMOUNT`.", parse_mode="Markdown")
        return
    try:
        amount = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Enter a valid amount.")
        return
    if amount <= 0:
        return
    sender = wallet(update.effective_user.id)
    receiver = wallet(target.id)
    if sender["coins"] < amount:
        await update.effective_message.reply_text("Not enough coins.")
        return
    sender["coins"] -= amount
    receiver["coins"] += amount
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text(f"💸 {user_line(update.effective_user)} sent `{amount}` coins to {user_line(target)}.", parse_mode="Markdown")

async def stats(update, context):
    u = wallet(update.effective_user.id)
    await update.effective_message.reply_text(
        f"📊 *Stats*\n\nCoins: `{u['coins']}`\nGems: `{u['gems']}`",
        parse_mode="Markdown"
    )

async def save_cmd(update, context):
    save_json(DATA_FILE, data)
    await update.effective_message.reply_text("💾 Data saved.")

async def coupons(update, context):
    await update.effective_message.reply_text("🎟️ No public coupons are configured.")

async def protect(update, context):
    await update.effective_message.reply_text("🛡️ Your balance is protected from automated transfer commands.")

async def rank(update, context):
    users = sorted(
        ((uid, wallet(int(uid))["coins"]) for uid in data["users"]),
        key=lambda x: x[1], reverse=True
    )[:10]
    if not users:
        await update.effective_message.reply_text("No economy data yet.")
        return
    lines = ["🏆 *Top Coins*",""]
    for i,(uid,coins) in enumerate(users,1):
        name = data["users"][uid].get("name","User")
        lines.append(f"{i}. {name} — `{coins}`")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def items(update, context):
    await update.effective_message.reply_text("🎒 Items are cosmetic in this test build. No purchases are required.")

async def gift(update, context):
    await give(update, context)

# ------------------------------------------------------------
# Games (non-gambling)
# ------------------------------------------------------------

async def minigames(update, context):
    await update.effective_message.reply_text(
        "🎮 *Mini Games*\n\n"
        "`/rps rock|paper|scissors`\n"
        "`/guess 1-10`\n"
        "`/puzzle`\n"
        "`/ludo` — roll a practice die",
        parse_mode="Markdown"
    )

async def rps(update, context):
    if not context.args:
        await update.effective_message.reply_text("Use `/rps rock`, `/rps paper` or `/rps scissors`.", parse_mode="Markdown")
        return
    choice = context.args[0].lower()
    if choice not in ("rock","paper","scissors"):
        await update.effective_message.reply_text("Choose rock, paper or scissors.")
        return
    bot = random.choice(("rock","paper","scissors"))
    if choice == bot:
        result = "Draw!"
    elif (choice,bot) in (("rock","scissors"),("paper","rock"),("scissors","paper")):
        result = "You win!"
    else:
        result = "Bot wins!"
    await update.effective_message.reply_text(f"🎮 You: `{choice}`\nNFC: `{bot}`\n\n{result}", parse_mode="Markdown")

async def guess(update, context):
    if not context.args:
        await update.effective_message.reply_text("Use `/guess 1-10`.", parse_mode="Markdown")
        return
    try:
        n=int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Enter a number from 1 to 10.")
        return
    if n < 1 or n > 10:
        await update.effective_message.reply_text("Number must be from 1 to 10.")
        return
    answer=random.randint(1,10)
    await update.effective_message.reply_text("🎯 Correct!" if n==answer else f"🎯 Not this time — I chose `{answer}`.", parse_mode="Markdown")

async def puzzle(update, context):
    puzzles=[
        ("I have keys but no locks. What am I?", "keyboard"),
        ("What has a face and two hands but no arms?", "clock"),
        ("What gets wetter as it dries?", "towel"),
    ]
    q,a=random.choice(puzzles)
    await update.effective_message.reply_text(f"🧩 *Puzzle*\n\n{q}\n\nAnswer: `{a}`", parse_mode="Markdown")

async def ludo(update, context):
    await update.effective_message.reply_text(f"🎲 Practice roll: **{random.randint(1,6)}**", parse_mode="Markdown")

# ------------------------------------------------------------
# Social / reply commands
# ------------------------------------------------------------

async def reply_reaction(update, context):
    target = reply_target(update)
    actor = update.effective_user
    cmd = update.effective_message.text.split()[0].split("@")[0].lower()

    if not target:
        await update.effective_message.reply_text(
            f"↩️ Reply to a user's message, then use {command_link(cmd)}.",
            parse_mode="HTML"
        )
        return

    # Every action gets its own emoji + custom quote/verb.
    labels = {
        "/brain": ("🧠", "gave", lambda: f"a brain score of {random.randint(40,100)}/100"),
        "/stupid_meter": ("🤓", "measured", lambda: f"a silly meter of {random.randint(0,100)}%"),
        "/detail": ("🔎", "opened the profile of", None),
        "/truth": ("🗣️", "sent a truth to", None),
        "/dare": ("🎯", "sent a safe dare to", None),
        "/poke": ("👉", "playfully poked", None),
        "/bonk": ("💫", "gave a cartoon bonk to", None),
        "/slap": ("👋", "slapped", None),
        "/hug": ("🫂", "gave a friendly hug to", None),
        "/care": ("🌷", "showed some care to", None),
        "/laugh": ("😂", "laughed at", None),
        "/funny": ("🤣", "sent a funny reaction to", None),
        "/wow": ("😮", "was amazed by", None),
    }

    emoji, action, value_fn = labels.get(cmd, ("✨", "reacted to", None))

    if cmd == "/detail":
        username = f"@{target.username}" if target.username else "no username"
        text = (
            f"{user_line(actor, emoji)} <b>{escape(action)}</b> {user_line(target, emoji)}\n\n"
            f"🔎 <b>User Detail</b>\n"
            f"Name: {user_line(target, '👤')}\n"
            f"Username: <code>{escape(username)}</code>\n"
            f"ID: <code>{target.id}</code>"
        )
    elif cmd == "/truth":
        text = (
            f"{user_line(actor, '🗣️')} <b>{escape(action)}</b> {user_line(target, '🎯')}\n\n"
            f"💬 <b>Truth:</b> What is one hobby you could talk about for hours?"
        )
    elif cmd == "/dare":
        text = (
            f"{user_line(actor, '🎯')} <b>{escape(action)}</b> {user_line(target, '🎯')}\n\n"
            f"🎯 <b>Dare:</b> Send the funniest sticker you have."
        )
    elif value_fn:
        text = (
            f"{user_line(actor, emoji)} <b>{escape(action)}</b> "
            f"{user_line(target, emoji)} <b>→</b> <code>{escape(value_fn())}</code>"
        )
    else:
        text = f"{user_line(actor, emoji)} <b>{escape(action)}</b> {user_line(target, emoji)}"

    # Reply to the original message so Telegram keeps the quoted-message layout.
    await update.effective_message.reply_text(text, parse_mode="HTML")

async def kick_user(update, context):
    """Admin-only kick. Works by banning and immediately unbanning the replied user."""
    if not await require_admin(update):
        return

    target = reply_target(update)
    if not target:
        await update.effective_message.reply_text(
            "↩️ Reply to the user's message with /kick (or /remove)."
        )
        return

    actor = update.effective_user
    chat = update.effective_chat

    if target.id == actor.id:
        await update.effective_message.reply_text("😅 You can't kick yourself with this command.")
        return
    if target.id == context.bot.id:
        await update.effective_message.reply_text("🤖 I can't kick myself.")
        return

    try:
        target_member = await chat.get_member(target.id)
        if target_member.status in ("administrator", "creator"):
            await update.effective_message.reply_text("🛡️ Admins/owners cannot be kicked by this command.")
            return

        # Telegram requires admin rights for this action.
        await context.bot.ban_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            revoke_messages=False,
        )
        await context.bot.unban_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            only_if_banned=True,
        )

        # Same Grand_Flea pack requested for kick.
        ids = await load_sticker_ids(context.bot, "kick")
        if ids:
            await update.effective_message.reply_sticker(random.choice(ids))

        text = (
            f"👢 {user_line(actor, '👢')} <b>kicked</b> "
            f"{user_line(target, '👢')}\n"
            f"↪️ <i>Removed from the group — they can join again if the group allows it.</i>"
        )
        await update.effective_message.reply_text(text, parse_mode="HTML")
    except TelegramError as e:
        logging.warning("Kick failed: %s", e)
        await update.effective_message.reply_text(
            "❌ I couldn't kick that user. Make sure NFC is an admin with permission to restrict members."
        )

async def friends(update, context):
    await update.effective_message.reply_text(
        "👥 *Friends*\nReply to a user's message with `/friends` to save them as a friend.",
        parse_mode="Markdown"
    )

async def duo(update, context):
    # Neutral random duo instead of romantic pairing.
    candidates = []
    for uid, info in data.get("users", {}).items():
        if not info.get("id"):
            continue
        candidates.append(info)
    if update.effective_user:
        remember_user(update.effective_user)
        candidates = [x for x in candidates if x.get("id") != update.effective_user.id]
    if len(candidates) < 1:
        await update.effective_message.reply_text(
            "🌸 *Random Duo*\n\nI need at least one other user who has interacted with NFC before.",
            parse_mode="Markdown"
        )
        return
    other = random.choice(candidates)
    actor = update.effective_user
    await update.effective_message.reply_text(
        "🌸 *TODAY'S RANDOM DUO* 🌸\n\n"
        f"{user_line(actor)}  ×  {user_line(type('U', (), {'id': other['id'], 'full_name': other['name'], 'username': other.get('username','')})())}\n\n"
        "A random team-up from the group — no tagging or notifications.",
        parse_mode="Markdown"
    )

# ------------------------------------------------------------
# Sticker system
# ------------------------------------------------------------

async def load_sticker_ids(bot, command):
    if command in sticker_cache and sticker_cache[command]:
        return sticker_cache[command]
    pack = STICKER_PACKS.get(command)
    if not pack:
        return []
    try:
        s = await bot.get_sticker_set(pack)
        ids = [x.file_id for x in s.stickers]
        if ids:
            sticker_cache[command] = ids
            save_json(STICKER_CACHE_FILE, sticker_cache)
        return ids
    except TelegramError as e:
        logging.warning("Sticker pack %s failed: %s", pack, e)
        return []

async def sticker_reaction(update, context):
    msg = update.effective_message
    if not msg or not msg.text or not msg.text.startswith("/"):
        return
    cmd = msg.text.split()[0].split("@")[0].lower().lstrip("/")
    if cmd not in STICKER_PACKS:
        return
    ids = await load_sticker_ids(context.bot, cmd)
    if not ids:
        await msg.reply_text(f"Sticker pack for `/{cmd}` could not be loaded.", parse_mode="Markdown")
        return
    await msg.reply_sticker(random.choice(ids))
    target = reply_target(update)
    actor = update.effective_user
    if target:
        emoji_map = {
            "love":"❤️", "hug":"🫂", "kiss":"💋", "miss":"🥺", "care":"🌷",
            "crush":"💗", "blush":"😊", "shy":"🙈", "laugh":"😂", "funny":"🤣",
            "troll":"😈", "wtf":"😵", "wow":"😮", "bruh":"😐", "aura":"✨",
            "swag":"😎", "dance":"💃", "happy":"😄", "sad":"😔", "cry":"😢",
            "mad":"😤", "scared":"😨", "excited":"🤩", "welcome":"🌸", "bye":"👋",
            "sorry":"🙏", "slap":"👋", "kick":"👢", "remove":"👢"
        }
        action_map = {
            "love":"sent a reaction to", "hug":"hugged", "kiss":"sent a reaction to",
            "miss":"missed", "care":"showed care to", "crush":"sent a reaction to",
            "blush":"blushed at", "shy":"got shy around", "laugh":"laughed at",
            "funny":"sent a funny reaction to", "troll":"trolled", "wtf":"reacted to",
            "wow":"reacted with wow to", "bruh":"reacted to", "aura":"showed aura to",
            "swag":"showed swag to", "dance":"danced for", "happy":"sent happiness to",
            "sad":"sent a sad reaction to", "cry":"cried at", "mad":"reacted angrily to",
            "scared":"got scared by", "excited":"got excited with", "welcome":"welcomed",
            "bye":"said bye to", "sorry":"said sorry to", "slap":"slapped"
        }
        emoji = emoji_map.get(cmd, "✨")
        action = action_map.get(cmd, "reacted to")
        await msg.reply_text(
            f"{user_line(actor, emoji)} <b>{escape(action)}</b> {user_line(target, emoji)}",
            parse_mode="HTML"
        )

# ------------------------------------------------------------
# Couple command intentionally replaced by neutral duo
# ------------------------------------------------------------

async def couple_alias(update, context):
    await duo(update, context)

# ------------------------------------------------------------
# Link moderation
# ------------------------------------------------------------

async def moderate(update, context):
    msg=update.effective_message
    chat=update.effective_chat
    if not msg or not chat or chat.type not in ("group","supergroup"):
        return
    if msg.from_user:
        remember_user(msg.from_user)
        save_json(DATA_FILE, data)
    c=get_chat(chat.id)
    if not c["enabled"] or not msg.from_user:
        return
    if await is_admin(update, msg.from_user.id):
        return
    if str(msg.from_user.id) in c["whitelist"]:
        return
    text=msg.text or msg.caption or ""
    if has_url(text):
        try:
            await msg.delete()
        except TelegramError:
            pass

# ------------------------------------------------------------
# Commands
# ------------------------------------------------------------

async def command_menu(app):
    commands=[
        BotCommand("start","Open NFC"),
        BotCommand("help","Show all commands"),
        BotCommand("settings","View group settings"),
        BotCommand("links","Link protection on/off"),
        BotCommand("whitelist","Manage whitelist"),
        BotCommand("blacklist","Remove from whitelist"),
        BotCommand("intro","About NFC"),
        BotCommand("open","Open group mode"),
        BotCommand("close","Close group mode"),
        BotCommand("admin","Admin access"),
        BotCommand("admins","Show group admins"),
        BotCommand("own","Group owner info"),
        BotCommand("kick","Kick a replied user"),
        BotCommand("remove","Remove a replied user"),
        BotCommand("friends","Friends info"),
        BotCommand("duo","Random group duo"),
        BotCommand("economy","View economy"),
        BotCommand("gems","View gems"),
        BotCommand("convert","Convert coins"),
        BotCommand("daily","Daily reward"),
        BotCommand("claim10k","Claim starter reward"),
        BotCommand("coupons","View coupons"),
        BotCommand("bal","View balance"),
        BotCommand("stats","View stats"),
        BotCommand("save","Save data"),
        BotCommand("give","Give coins"),
        BotCommand("protect","Protection info"),
        BotCommand("rank","Coin leaderboard"),
        BotCommand("items","View items"),
        BotCommand("gift","Give coins"),
        BotCommand("minigames","Game list"),
        BotCommand("ludo","Roll practice die"),
        BotCommand("puzzle","Puzzle"),
        BotCommand("rps","Rock paper scissors"),
        BotCommand("guess","Number guess"),
        BotCommand("truth","Truth prompt"),
        BotCommand("dare","Dare prompt"),
        BotCommand("detail","User details"),
        BotCommand("brain","Brain score"),
        BotCommand("stupid_meter","Silly meter"),
        BotCommand("translate","Translation info"),
        BotCommand("couple","Use /duo for a neutral random duo"),
    ]
    await app.bot.set_my_commands(commands)

async def translate_cmd(update, context):
    await update.effective_message.reply_text(
        "🌐 Translation: send `/translate` followed by text, e.g. `/translate hello`.\n"
        "This local build doesn't call an external translation service."
    )

def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN environment variable is missing.")
        return

    app=(Application.builder().token(BOT_TOKEN).post_init(command_menu).build())

    handlers = {
        "start": start, "help": help_command, "settings": settings,
        "links": links, "whitelist": whitelist, "blacklist": blacklist,
        "intro": intro, "open": open_group, "close": close_group,
        "admin": admin_info, "admins": admins, "own": own,
        "friends": friends, "duo": duo, "couple": couple_alias,
        "kick": kick_user, "remove": kick_user,
        "economy": economy, "gems": gems, "convert": convert,
        "daily": daily, "claim10k": claim10k, "coupons": coupons,
        "bal": bal, "stats": stats, "save": save_cmd, "give": give,
        "protect": protect, "rank": rank, "items": items, "gift": gift,
        "minigames": minigames, "ludo": ludo, "rps": rps, "guess": guess,
        "puzzle": puzzle, "translate": translate_cmd,
    }
    for name, fn in handlers.items():
        app.add_handler(CommandHandler(name, fn))

    # Reply-based safe commands.
    for name in ["brain","stupid_meter","detail","truth","dare","poke","bonk","slap","hug","care","laugh","funny","wow"]:
        app.add_handler(CommandHandler(name, reply_reaction))

    # Sticker commands.
    app.add_handler(MessageHandler(filters.COMMAND, sticker_reaction))

    # Track users and protect links.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate))

    # Render free Web Service needs an HTTP listener.
    # This endpoint is only a health check; the Telegram bot still uses polling.
    web = Flask(__name__)

    @web.get("/")
    def health():
        return "NFC bot is running", 200

    @web.get("/health")
    def health_check():
        return {"status": "ok", "bot": "NFC"}, 200

    port = int(os.getenv("PORT", "10000"))
    Thread(target=lambda: web.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True).start()

    print("NFC started on Render.")
    print("Protection: ON")
    print("Sticker reactions: ON")
    print("Economy/games: ON")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
