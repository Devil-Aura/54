import subprocess
import sys
import os

# ──────────────────────────────────────────────
#  AUTO-LOAD .env  (before anything else)
# ──────────────────────────────────────────────
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ──────────────────────────────────────────────
#  AUTO-INSTALL REQUIREMENTS
# ──────────────────────────────────────────────
_req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
if os.path.exists(_req_file):
    print("📦 Checking and installing requirements…")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "-r", _req_file],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    print("✅ Requirements ready.")

import re
import logging
from datetime import datetime

from telegram import (
    Update, MessageEntity,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler,
)
from telegram.error import TelegramError
from pymongo import MongoClient

# ──────────────────────────────────────────────
#  CONFIG  (set via environment or .env file)
# ──────────────────────────────────────────────
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
FORCE_SUB_CHANNEL = int(os.getenv("FORCE_SUB_CHANNEL", "-1002432405855"))
MONGO_URI         = os.getenv(
    "MONGO_URI",
    "mongodb+srv://cover:cover0123@cluster0.oilx4yu.mongodb.net/?appName=Cluster0",
)
# Owner and admins receive a DM when the bot starts/restarts
# Set OWNER_ID and ADMIN_IDS (comma-separated) in your .env file
OWNER_ID   = int(os.getenv("OWNER_ID", "0"))
ADMIN_IDS  = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]

# ──────────────────────────────────────────────
#  MONGODB
# ──────────────────────────────────────────────
_client       = MongoClient(MONGO_URI)
_db           = _client["video_cover_bot"]
covers_col    = _db["covers"]          # per-chat/season anime covers
pending_col   = _db["pending"]         # awaiting cover image in channel
states_col    = _db["user_states"]     # DM user states + personal thumbnail
channels_col  = _db["managed_channels"]  # bot-managed channels

# indexes
covers_col.create_index(
    [("chat_id", 1), ("anime_name_lower", 1), ("season", 1)], unique=True
)
pending_col.create_index([("chat_id", 1), ("bot_msg_id", 1)], unique=True)
states_col.create_index("user_id", unique=True)
channels_col.create_index([("owner_id", 1), ("chat_id", 1)], unique=True)

# ──────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot_errors.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  CONSTANTS
# ──────────────────────────────────────────────
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".3gp")
COVERS_PER_PAGE  = 8
CHANS_PER_PAGE   = 6
DEFAULT_SEASON   = "S01"

# ──────────────────────────────────────────────
#  SEASON UTILITIES
# ──────────────────────────────────────────────
_SEASON_RE = re.compile(r'\b[Ss](?:eason\s*)?(\d{1,2})\b')

def parse_season(text: str) -> tuple[str, str]:
    """
    Extract season tag from text.
    Returns (clean_anime_name, season_tag)  e.g. ("Naruto", "S01")
    Defaults to S01 if no season found.
    """
    m = _SEASON_RE.search(text)
    if m:
        num = int(m.group(1))
        season = f"S{num:02d}"
        name = _SEASON_RE.sub("", text).strip()
        return name.strip(), season
    return text.strip(), DEFAULT_SEASON

def full_anime_key(anime_name: str, season: str) -> str:
    """Human-readable key like 'Naruto S01'."""
    return f"{anime_name.strip()} {season}"

# ──────────────────────────────────────────────
#  SMALL HELPERS
# ──────────────────────────────────────────────
def get_msg(update: Update):
    """Return whichever message object is present."""
    return update.message or update.channel_post

def get_effective_msg(update: Update):
    """Return message, channel_post, edited_message, or edited_channel_post."""
    return (
        update.message
        or update.channel_post
        or update.edited_message
        or update.edited_channel_post
    )

def is_video_document(doc) -> bool:
    if doc is None:
        return False
    mime = doc.mime_type or ""
    name = (doc.file_name or "").lower()
    return mime.startswith("video/") or name.endswith(VIDEO_EXTENSIONS)

def serialize_entities(entities) -> list:
    if not entities:
        return []
    return [
        {
            "type": e.type,
            "offset": e.offset,
            "length": e.length,
            "user": e.user.to_dict() if e.user else None,
        }
        for e in entities
    ]

def deserialize_entities(data: list):
    if not data:
        return None
    return [
        MessageEntity(
            type=e["type"],
            offset=e["offset"],
            length=e["length"],
            user=e.get("user"),
        )
        for e in data
    ] or None

# ──────────────────────────────────────────────
#  DB HELPERS – covers
# ──────────────────────────────────────────────
def db_save_cover(chat_id: int, chat_name: str, anime_name: str, season: str, file_id: str):
    key_lower = anime_name.strip().lower()
    covers_col.update_one(
        {"chat_id": chat_id, "anime_name_lower": key_lower, "season": season},
        {"$set": {
            "chat_id":          chat_id,
            "chat_name":        chat_name,
            "anime_name":       anime_name.strip(),
            "anime_name_lower": key_lower,
            "season":           season,
            "file_id":          file_id,
            "updated_at":       datetime.utcnow(),
        }},
        upsert=True,
    )

def db_get_cover(chat_id: int, anime_name: str, season: str):
    return covers_col.find_one({
        "chat_id": chat_id,
        "anime_name_lower": anime_name.strip().lower(),
        "season": season,
    })

def db_get_cover_best(chat_id: int, anime_name: str, season: str):
    """Try exact season first, fall back to any season for that anime."""
    doc = db_get_cover(chat_id, anime_name, season)
    if doc:
        return doc
    return covers_col.find_one({
        "chat_id": chat_id,
        "anime_name_lower": anime_name.strip().lower(),
    })

def db_all_covers(chat_id: int) -> list:
    return list(covers_col.find({"chat_id": chat_id}).sort([("anime_name", 1), ("season", 1)]))

def db_del_cover_by_key(chat_id: int, anime_name: str, season: str) -> bool:
    r = covers_col.delete_one({
        "chat_id": chat_id,
        "anime_name_lower": anime_name.strip().lower(),
        "season": season,
    })
    return r.deleted_count > 0

def db_del_cover_index(chat_id: int, idx: int):
    covers = db_all_covers(chat_id)
    if 1 <= idx <= len(covers):
        doc = covers[idx - 1]
        covers_col.delete_one({"_id": doc["_id"]})
        return full_anime_key(doc["anime_name"], doc["season"])
    return None

def db_del_all_covers_for_chat(chat_id: int) -> int:
    r = covers_col.delete_many({"chat_id": chat_id})
    return r.deleted_count

def db_search_covers(chat_id: int, query: str) -> list:
    q = query.strip().lower()
    return list(covers_col.find({
        "chat_id": chat_id,
        "anime_name_lower": {"$regex": re.escape(q)},
    }).sort([("anime_name", 1), ("season", 1)]))

def db_channels_with_covers() -> list:
    pipeline = [
        {"$group": {
            "_id": "$chat_id",
            "chat_name": {"$first": "$chat_name"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"chat_name": 1}},
    ]
    return list(covers_col.aggregate(pipeline))

# ──────────────────────────────────────────────
#  DB HELPERS – pending
# ──────────────────────────────────────────────
def db_save_pending(chat_id, bot_msg_id, video_msg_id, file_id,
                    caption, entities, has_spoiler, is_doc):
    pending_col.update_one(
        {"chat_id": chat_id, "bot_msg_id": bot_msg_id},
        {"$set": {
            "chat_id":       chat_id,
            "bot_msg_id":    bot_msg_id,
            "video_msg_id":  video_msg_id,
            "file_id":       file_id,
            "caption":       caption,
            "entities":      entities,
            "has_spoiler":   has_spoiler,
            "is_doc":        is_doc,
            "ts":            datetime.utcnow(),
        }},
        upsert=True,
    )

def db_get_pending(chat_id, bot_msg_id):
    return pending_col.find_one({"chat_id": chat_id, "bot_msg_id": bot_msg_id})

def db_del_pending(chat_id, bot_msg_id):
    pending_col.delete_one({"chat_id": chat_id, "bot_msg_id": bot_msg_id})

# ──────────────────────────────────────────────
#  DB HELPERS – user states
# ──────────────────────────────────────────────
def db_get_state(user_id: int) -> dict:
    return states_col.find_one({"user_id": user_id}) or {}

def db_set_state(user_id: int, patch: dict):
    states_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, **patch}},
        upsert=True,
    )

# ──────────────────────────────────────────────
#  DB HELPERS – managed channels
# ──────────────────────────────────────────────
def db_add_channel(owner_id: int, chat_id: int, chat_name: str):
    channels_col.update_one(
        {"owner_id": owner_id, "chat_id": chat_id},
        {"$set": {"owner_id": owner_id, "chat_id": chat_id,
                  "chat_name": chat_name, "added_at": datetime.utcnow()}},
        upsert=True,
    )

def db_remove_channel(owner_id: int, chat_id: int) -> bool:
    r = channels_col.delete_one({"owner_id": owner_id, "chat_id": chat_id})
    return r.deleted_count > 0

def db_get_user_channels(owner_id: int) -> list:
    return list(channels_col.find({"owner_id": owner_id}).sort("chat_name", 1))

# ──────────────────────────────────────────────
#  ANIME DETECTION
# ──────────────────────────────────────────────
def find_anime_in_text(text: str, known: list) -> tuple[str, str] | None:
    """Match known anime names (with season) against text. Returns (name, season) or None."""
    if not text:
        return None
    text_l = text.lower()
    for doc in known:
        name   = doc["anime_name"]
        season = doc.get("season", DEFAULT_SEASON)
        if name.lower() in text_l:
            return name, season
    return None

# ──────────────────────────────────────────────
#  FORCE SUB
# ──────────────────────────────────────────────
async def is_joined(user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await ctx.bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

# ──────────────────────────────────────────────
#  CHAT NAME UTIL
# ──────────────────────────────────────────────
async def chat_name_for(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    try:
        c = await ctx.bot.get_chat(chat_id)
        return c.title or c.full_name or str(chat_id)
    except Exception:
        return str(chat_id)

# ──────────────────────────────────────────────
#  SEND VIDEO / DOCUMENT HELPER
# ──────────────────────────────────────────────
async def send_with_cover(ctx, chat_id, file_id, cover_file_id,
                          caption, entities, has_spoiler, reply_to, is_doc):
    if is_doc:
        await ctx.bot.send_document(
            chat_id=chat_id,
            document=file_id,
            thumbnail=cover_file_id,
            caption=caption,
            caption_entities=entities,
            reply_to_message_id=reply_to,
        )
    else:
        # Use api_kwargs to pass 'cover' directly to Telegram Bot API
        # (PTB 21.x exposes it as api_kwargs; PTB 22.x has it as a native param)
        await ctx.bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=caption,
            caption_entities=entities,
            supports_streaming=True,
            has_spoiler=has_spoiler,
            reply_to_message_id=reply_to,
            api_kwargs={"cover": cover_file_id},
        )

# ──────────────────────────────────────────────
#  UI BUILDERS
# ──────────────────────────────────────────────
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Cover",      callback_data="menu:add_cover"),
            InlineKeyboardButton("📚 Manage Covers",  callback_data="menu:covers"),
        ],
        [
            InlineKeyboardButton("🗂 Manage Channels", callback_data="menu:channels"),
            InlineKeyboardButton("🖼 My Thumbnail",    callback_data="menu:thumb"),
        ],
        [
            InlineKeyboardButton("📊 Statistics",     callback_data="menu:stats"),
            InlineKeyboardButton("❓ Help",            callback_data="menu:help"),
        ],
    ])

def back_to_main_btn() -> list:
    return [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main")]

def covers_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 List Covers",    callback_data=f"cov:list:{chat_id}:0"),
            InlineKeyboardButton("🔍 Search Cover",   callback_data=f"cov:search_prompt:{chat_id}"),
        ],
        [
            InlineKeyboardButton("❌ Delete Cover",   callback_data=f"cov:del_prompt:{chat_id}"),
            InlineKeyboardButton("🗑 Delete All",     callback_data=f"cov:delall_confirm:{chat_id}"),
        ],
        back_to_main_btn(),
    ])

def channels_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Channel",    callback_data="chan:add_prompt"),
            InlineKeyboardButton("📋 My Channels",    callback_data="chan:list:0"),
        ],
        [
            InlineKeyboardButton("❌ Remove Channel", callback_data="chan:remove_prompt"),
            InlineKeyboardButton("✅ Verify Perms",   callback_data="chan:verify_prompt"),
        ],
        back_to_main_btn(),
    ])

def _build_channel_list_keyboard(channels: list, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total       = len(channels)
    total_pages = max(1, (total + CHANS_PER_PAGE - 1) // CHANS_PER_PAGE)
    start       = page * CHANS_PER_PAGE
    items       = channels[start: start + CHANS_PER_PAGE]

    rows = []
    pair = []
    for ch in items:
        label = f"📺 {ch.get('chat_name', ch['_id'])} ({ch['count']})"
        pair.append(InlineKeyboardButton(label, callback_data=f"ch:{ch['_id']}:0"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cl:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cl:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append(back_to_main_btn())

    text = (
        f"📋 <b>Chats with saved covers</b>\n\n"
        f"Page {page+1}/{total_pages}  •  {total} chat(s)"
    )
    return text, InlineKeyboardMarkup(rows)

def _build_covers_page_keyboard(cid: int, covers: list, page: int, per_page: int = 10):
    total       = len(covers)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start       = page * per_page
    chunk       = covers[start: start + per_page]
    cname       = covers[0]["chat_name"] if covers else str(cid)

    lines = "\n".join(
        f"{start+i+1}. <b>{c['anime_name']}</b> {c.get('season', DEFAULT_SEASON)}"
        for i, c in enumerate(chunk)
    )
    text = (
        f"🎌 <b>Covers – {cname}</b>\n\n"
        f"{lines}\n\n"
        f"📊 Total: {total}  •  Page {page+1}/{total_pages}"
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"ch:{cid}:{page-1}"))
    nav.append(InlineKeyboardButton("🔙 Back", callback_data="cl:0"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"ch:{cid}:{page+1}"))

    return text, InlineKeyboardMarkup([nav])

# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return

    text = (
        "🎬 <b>Anime Cover Bot</b>\n\n"
        "Welcome! I can automatically apply anime covers to your channel videos.\n\n"
        "Choose an option below to get started:"
    )

    if msg.chat.type == "private":
        await msg.reply_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    else:
        await msg.reply_text(text, parse_mode="HTML")

# ══════════════════════════════════════════════
#  /help
# ══════════════════════════════════════════════
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return
    text = (
        "🎬 <b>Anime Cover Bot – Help</b>\n\n"
        "<b>📺 Channel workflow:</b>\n"
        "1. Reply to an image with <code>/cover Naruto S01</code>\n"
        "2. Post any video — bot checks filename &amp; caption\n"
        "3. If anime name matches → cover auto-applied\n"
        "4. If no match → bot asks for a cover image\n"
        "5. Reply to that message with an image → applied!\n\n"
        "<b>💬 DM workflow:</b>\n"
        "• Send a photo → saved as your personal thumbnail\n"
        "• Send a video → thumbnail applied instantly\n"
        "• <code>/cover Naruto S01 -1001234567890</code> → add cover for a channel from DM\n\n"
        "<b>🌸 Season support:</b>\n"
        "• Default season is <b>S01</b> if not specified\n"
        "• <code>/cover Naruto S02</code> saves a different cover per season\n\n"
        "<b>🛠 Commands:</b>\n"
        "<code>/cover [name] [season] [channel_id?]</code> — Save cover\n"
        "<code>/allcovers</code> — List covers in this chat\n"
        "<code>/delcover [name] [season]</code> — Delete a cover\n"
        "<code>/listcover</code> — Browse all chats with covers\n"
        "<code>/mythumb</code> — View your thumbnail\n"
        "<code>/delthumb</code> — Remove your thumbnail\n\n"
        "⚡ <b>Powered by:</b> @World_Fastest_Bots"
    )
    kb = InlineKeyboardMarkup([back_to_main_btn()]) if msg.chat.type == "private" else None
    await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

# ══════════════════════════════════════════════
#  /cover  (channels + DM + DM→channel)
# ══════════════════════════════════════════════
async def cmd_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return

    if not ctx.args:
        await msg.reply_text(
            "❌ <b>Usage:</b>\n"
            "• In channel: <code>/cover Naruto S01</code> (reply to image)\n"
            "• In DM for channel: <code>/cover Naruto S01 -1001234567890</code>",
            parse_mode="HTML",
        )
        return

    args = ctx.args[:]

    # ── DM → specific channel ──────────────────
    target_chat_id = None
    if msg.chat.type == "private" and args and args[-1].lstrip("-").isdigit():
        target_chat_id = int(args.pop())

    raw_name_season = " ".join(args).strip()
    anime_name, season = parse_season(raw_name_season)

    if not anime_name:
        await msg.reply_text("❌ Please provide an anime name.")
        return

    # ── Determine target chat ──────────────────
    if target_chat_id:
        # Verify bot is admin in that channel
        try:
            bot_member = await ctx.bot.get_chat_member(target_chat_id, ctx.bot.id)
            if bot_member.status not in ("administrator", "creator"):
                await msg.reply_text("❌ I'm not an admin in that channel.")
                return
        except TelegramError as e:
            await msg.reply_text(f"❌ Can't access that channel: {e}")
            return

        cid = target_chat_id
    else:
        cid = msg.chat_id

    # ── Get image from reply ───────────────────
    if not msg.reply_to_message:
        await msg.reply_text(
            "❌ Please <b>reply to an image</b> with this command.",
            parse_mode="HTML",
        )
        return

    replied = msg.reply_to_message
    file_id = None
    if replied.photo:
        file_id = replied.photo[-1].file_id
    elif replied.document and replied.document.mime_type and replied.document.mime_type.startswith("image/"):
        file_id = replied.document.file_id
    elif replied.sticker:
        file_id = replied.sticker.file_id

    if not file_id:
        await msg.reply_text("❌ The replied message must contain a photo, image document, or sticker.")
        return

    cname = await chat_name_for(cid, ctx)
    db_save_cover(cid, cname, anime_name, season, file_id)

    channel_note = f"📺 Channel: <b>{cname}</b>" if target_chat_id else f"📺 Chat: <b>{cname}</b>"
    await msg.reply_text(
        f"✅ <b>Cover saved!</b>\n\n"
        f"🎌 Anime: <b>{anime_name}</b>\n"
        f"🔖 Season: <b>{season}</b>\n"
        f"{channel_note}\n\n"
        f"This cover will be applied when a video matching "
        f"<b>{anime_name}</b> is posted there.",
        parse_mode="HTML",
    )

# ══════════════════════════════════════════════
#  /allcovers
# ══════════════════════════════════════════════
async def cmd_allcovers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return
    cid   = msg.chat_id
    cname = await chat_name_for(cid, ctx)
    covers = db_all_covers(cid)

    if not covers:
        await msg.reply_text(
            f"❌ No covers saved for <b>{cname}</b> yet.\n"
            "Use <code>/cover [name] [season]</code> (reply to image) to add one.",
            parse_mode="HTML",
        )
        return

    per_page = 10
    total = len(covers)
    chunk = covers[:per_page]
    lines = "\n".join(
        f"{i}. <b>{c['anime_name']}</b> {c.get('season', DEFAULT_SEASON)}"
        for i, c in enumerate(chunk, 1)
    )
    nav = []
    if total > per_page:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"ch:{cid}:1"))
    kb = InlineKeyboardMarkup([nav]) if nav else None

    await msg.reply_text(
        f"🎌 <b>Covers in {cname}</b>\n\n{lines}\n\n"
        f"📊 Total: {total}",
        parse_mode="HTML",
        reply_markup=kb,
    )

# ══════════════════════════════════════════════
#  /delcover  [name] [season?]
# ══════════════════════════════════════════════
async def cmd_delcover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return
    if not ctx.args:
        await msg.reply_text(
            "❌ <b>Usage:</b>\n"
            "<code>/delcover Naruto S01</code> — delete by name+season\n"
            "<code>/delcover 3</code> — delete by list number",
            parse_mode="HTML",
        )
        return

    arg  = " ".join(ctx.args).strip()
    cid  = msg.chat_id

    if arg.isdigit():
        deleted = db_del_cover_index(cid, int(arg))
        if deleted:
            await msg.reply_text(f"✅ Deleted cover <b>#{arg}</b>: <b>{deleted}</b>", parse_mode="HTML")
        else:
            await msg.reply_text("❌ Invalid number. Use /allcovers to see the list.")
    else:
        anime_name, season = parse_season(arg)
        ok = db_del_cover_by_key(cid, anime_name, season)
        if ok:
            await msg.reply_text(
                f"✅ Deleted cover: <b>{anime_name} {season}</b>", parse_mode="HTML"
            )
        else:
            await msg.reply_text(
                f"❌ No cover found for: <b>{anime_name} {season}</b>", parse_mode="HTML"
            )

# ══════════════════════════════════════════════
#  /listcover  (paginated channel list)
# ══════════════════════════════════════════════
async def cmd_listcover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return
    channels = db_channels_with_covers()
    if not channels:
        await msg.reply_text("❌ No covers saved anywhere yet.")
        return
    text, kb = _build_channel_list_keyboard(channels, 0)
    await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ══════════════════════════════════════════════
#  /mythumb  &  /delthumb
# ══════════════════════════════════════════════
async def cmd_mythumb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        await msg.reply_text("❌ This command is only for personal DMs.")
        return
    thumb = db_get_state(uid).get("thumbnail")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Delete Thumbnail", callback_data="thumb:delete")],
        back_to_main_btn(),
    ])
    if thumb:
        try:
            await msg.reply_photo(
                photo=thumb,
                caption="🖼️ <b>Your current thumbnail</b>",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            await msg.reply_text("❌ Can't load thumbnail. Please send a new photo to set one.")
    else:
        await msg.reply_text(
            "❌ No thumbnail saved yet.\nSend me a photo to set one.",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
        )

async def cmd_delthumb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        return
    state = db_get_state(uid)
    if state.get("thumbnail"):
        db_set_state(uid, {"thumbnail": None})
        await msg.reply_text("✅ Thumbnail removed.")
    else:
        await msg.reply_text("❌ No thumbnail to remove.")

# ══════════════════════════════════════════════
#  CALLBACK QUERY HANDLER
# ══════════════════════════════════════════════
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data  = q.data
    uid   = q.from_user.id

    # ── Main Menu ──────────────────────────────
    if data == "menu:main":
        await q.edit_message_text(
            "🏠 <b>Main Menu</b>\n\nChoose an option:",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "menu:add_cover":
        await q.edit_message_text(
            "➕ <b>Add Cover</b>\n\n"
            "To add a cover, go to your channel or DM and:\n\n"
            "📺 <b>In channel:</b>\n"
            "Reply to an image with:\n"
            "<code>/cover Naruto S01</code>\n\n"
            "💬 <b>From DM (for a channel):</b>\n"
            "Reply to an image with:\n"
            "<code>/cover Naruto S01 -1001234567890</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
        )

    elif data == "menu:covers":
        channels = db_channels_with_covers()
        if not channels:
            await q.edit_message_text(
                "📚 <b>Manage Covers</b>\n\nNo covers saved anywhere yet.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
            )
            return
        text, kb = _build_channel_list_keyboard(channels, 0)
        await q.edit_message_text(
            "📚 <b>Cover Manager</b>\n\n" + text,
            parse_mode="HTML",
            reply_markup=kb,
        )

    elif data == "menu:channels":
        channels = db_get_user_channels(uid)
        count = len(channels)
        await q.edit_message_text(
            f"🗂 <b>Channel Manager</b>\n\n"
            f"You have <b>{count}</b> managed channel(s).\n\n"
            "Add your channel ID to manage it from here.\n"
            "To get a channel ID, forward any message from it to @userinfobot.",
            parse_mode="HTML",
            reply_markup=channels_menu_keyboard(),
        )

    elif data == "menu:thumb":
        thumb = db_get_state(uid).get("thumbnail")
        if thumb:
            try:
                await q.message.reply_photo(
                    photo=thumb,
                    caption="🖼️ <b>Your current thumbnail</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 Delete", callback_data="thumb:delete")],
                        back_to_main_btn(),
                    ]),
                )
                await q.message.delete()
                return
            except Exception:
                pass
        await q.edit_message_text(
            "🖼 <b>My Thumbnail</b>\n\nNo thumbnail saved.\nSend me a photo to set one.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
        )

    elif data == "menu:stats":
        total_covers   = covers_col.count_documents({})
        total_channels = len(db_channels_with_covers())
        user_covers    = covers_col.count_documents({"chat_id": uid})
        await q.edit_message_text(
            f"📊 <b>Statistics</b>\n\n"
            f"🎌 Total covers: <b>{total_covers}</b>\n"
            f"📺 Total channels: <b>{total_channels}</b>\n"
            f"👤 Your covers: <b>{user_covers}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
        )

    elif data == "menu:help":
        text = (
            "❓ <b>Help</b>\n\n"
            "<b>Channel workflow:</b>\n"
            "1. Reply to image: <code>/cover Naruto S01</code>\n"
            "2. Bot auto-applies cover when a matching video is posted\n\n"
            "<b>DM workflow:</b>\n"
            "• Send photo → set thumbnail\n"
            "• Send video → thumbnail applied\n"
            "• <code>/cover Naruto S01 -100123</code> → add channel cover from DM\n\n"
            "<b>Season:</b> Default is S01 if not specified\n\n"
            "⚡ @World_Fastest_Bots"
        )
        await q.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
        )

    # ── Thumbnail ──────────────────────────────
    elif data == "thumb:delete":
        db_set_state(uid, {"thumbnail": None})
        await q.edit_message_text(
            "✅ Thumbnail deleted.\n\nSend me a photo to set a new one.",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
        )

    # ── Channel list (cover browser) ───────────
    elif data.startswith("cl:"):
        page     = int(data[3:])
        channels = db_channels_with_covers()
        text, kb = _build_channel_list_keyboard(channels, page)
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    # ── Single channel covers ──────────────────
    elif data.startswith("ch:"):
        _, cid_s, page_s = data.split(":")
        cid    = int(cid_s)
        page   = int(page_s)
        covers = db_all_covers(cid)

        if not covers:
            await q.edit_message_text(
                "❌ No covers found for this channel.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="cl:0")]
                ]),
            )
            return

        text, kb = _build_covers_page_keyboard(cid, covers, page)
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    # ── Cover management (from DM panel) ──────
    elif data.startswith("cov:list:"):
        _, _, cid_s, page_s = data.split(":")
        cid    = int(cid_s)
        page   = int(page_s)
        covers = db_all_covers(cid)
        if not covers:
            await q.edit_message_text(
                "❌ No covers in this chat.",
                reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
            )
            return
        text, kb = _build_covers_page_keyboard(cid, covers, page)
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data.startswith("cov:delall_confirm:"):
        cid = int(data.split(":")[-1])
        count = covers_col.count_documents({"chat_id": cid})
        await q.edit_message_text(
            f"⚠️ <b>Are you sure?</b>\n\nThis will delete ALL <b>{count}</b> cover(s) for this chat.\nThis cannot be undone.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, delete all", callback_data=f"cov:delall_do:{cid}"),
                    InlineKeyboardButton("❌ Cancel",          callback_data="menu:main"),
                ]
            ]),
        )

    elif data.startswith("cov:delall_do:"):
        cid   = int(data.split(":")[-1])
        count = db_del_all_covers_for_chat(cid)
        await q.edit_message_text(
            f"✅ Deleted <b>{count}</b> cover(s) from this chat.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]),
        )

    # ── Channel management (DM panel) ─────────
    elif data == "chan:add_prompt":
        db_set_state(uid, {"state": "waiting_for_channel_id"})
        await q.edit_message_text(
            "🗂 <b>Add Channel</b>\n\n"
            "Please send me the <b>Channel ID</b>.\n\n"
            "To get your channel ID:\n"
            "• Forward any message from your channel to @userinfobot\n"
            "• It usually looks like <code>-1001234567890</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="menu:channels")]
            ]),
        )

    elif data == "chan:list:0":
        channels = db_get_user_channels(uid)
        if not channels:
            await q.edit_message_text(
                "📋 <b>My Channels</b>\n\nYou haven't added any channels yet.",
                parse_mode="HTML",
                reply_markup=channels_menu_keyboard(),
            )
            return
        lines = "\n".join(
            f"• <b>{ch['chat_name']}</b> (<code>{ch['chat_id']}</code>)"
            for ch in channels
        )
        await q.edit_message_text(
            f"📋 <b>My Channels</b>\n\n{lines}",
            parse_mode="HTML",
            reply_markup=channels_menu_keyboard(),
        )

    elif data == "chan:remove_prompt":
        channels = db_get_user_channels(uid)
        if not channels:
            await q.edit_message_text(
                "❌ You have no channels to remove.",
                reply_markup=channels_menu_keyboard(),
            )
            return
        rows = [
            [InlineKeyboardButton(
                f"❌ {ch['chat_name']}",
                callback_data=f"chan:remove_do:{ch['chat_id']}",
            )]
            for ch in channels
        ]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:channels")])
        await q.edit_message_text(
            "🗂 <b>Remove Channel</b>\n\nSelect a channel to remove:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("chan:remove_do:"):
        cid = int(data.split(":")[-1])
        ok  = db_remove_channel(uid, cid)
        msg_text = "✅ Channel removed." if ok else "❌ Channel not found."
        await q.edit_message_text(msg_text, reply_markup=channels_menu_keyboard())

    elif data == "chan:verify_prompt":
        channels = db_get_user_channels(uid)
        if not channels:
            await q.edit_message_text(
                "❌ You have no channels to verify.",
                reply_markup=channels_menu_keyboard(),
            )
            return
        rows = [
            [InlineKeyboardButton(
                f"✅ {ch['chat_name']}",
                callback_data=f"chan:verify_do:{ch['chat_id']}",
            )]
            for ch in channels
        ]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:channels")])
        await q.edit_message_text(
            "✅ <b>Verify Permissions</b>\n\nSelect a channel to check:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("chan:verify_do:"):
        cid = int(data.split(":")[-1])
        try:
            me = await ctx.bot.get_chat_member(cid, ctx.bot.id)
            chat = await ctx.bot.get_chat(cid)
            status = me.status
            can_post = getattr(me, "can_post_messages", False)
            can_edit = getattr(me, "can_edit_messages", False)
            cover_count = covers_col.count_documents({"chat_id": cid})
            await q.edit_message_text(
                f"✅ <b>Bot Status in {chat.title}</b>\n\n"
                f"• Role: <b>{status}</b>\n"
                f"• Can post: {'✅' if can_post else '❌'}\n"
                f"• Can edit: {'✅' if can_edit else '❌'}\n"
                f"• Saved covers: <b>{cover_count}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu:channels")]
                ]),
            )
        except TelegramError as e:
            await q.edit_message_text(
                f"❌ Cannot access channel: {e}",
                reply_markup=channels_menu_keyboard(),
            )

    # ── Force-sub check ────────────────────────
    elif data == "check_join":
        if await is_joined(uid, ctx):
            await q.edit_message_text(
                "✅ <b>Welcome!</b> You can now use the bot.\n\n⚡ @World_Fastest_Bots",
                parse_mode="HTML",
            )
        else:
            await q.answer("Please join the channel first!", show_alert=True)

# ══════════════════════════════════════════════
#  TEXT MESSAGE HANDLER  (for DM states)
# ══════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg or msg.chat.type != "private":
        return

    uid   = msg.from_user.id if msg.from_user else None
    if not uid:
        return

    state = db_get_state(uid)

    if state.get("state") == "waiting_for_channel_id":
        text = msg.text.strip()
        # Try to parse channel ID
        cid_str = text.lstrip("@")
        try:
            chat = await ctx.bot.get_chat(int(cid_str) if cid_str.lstrip("-").isdigit() else f"@{cid_str}")
            cname = chat.title or str(chat.id)
            # Check bot is admin
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                await msg.reply_text(
                    "❌ I'm not an administrator in that channel.\n"
                    "Please add me as admin first, then try again.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Back", callback_data="menu:channels")]
                    ]),
                )
                db_set_state(uid, {"state": "idle"})
                return
            db_add_channel(uid, chat.id, cname)
            db_set_state(uid, {"state": "idle"})
            await msg.reply_text(
                f"✅ <b>{cname}</b> added successfully!\n\n"
                "You can now add covers for this channel from DM.",
                parse_mode="HTML",
                reply_markup=channels_menu_keyboard(),
            )
        except (TelegramError, ValueError) as e:
            await msg.reply_text(
                f"❌ Couldn't find that channel: {e}\n\nMake sure I'm an admin there and the ID is correct.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="menu:channels")]
                ]),
            )

# ══════════════════════════════════════════════
#  CORE: process a video posted in channel
# ══════════════════════════════════════════════
async def _process_channel_video(ctx, msg, file_id, caption, entities_raw,
                                  has_spoiler, is_doc, file_name=""):
    chat_id = msg.chat_id

    search_texts = [t for t in [caption, file_name] if t]

    covers = db_all_covers(chat_id)
    found  = None

    if covers:
        for txt in search_texts:
            found = find_anime_in_text(txt, covers)
            if found:
                break

    if found:
        anime_name, season = found
        cover_doc = db_get_cover_best(chat_id, anime_name, season)
        if cover_doc:
            try:
                await send_with_cover(
                    ctx, chat_id, file_id,
                    cover_doc["file_id"],
                    caption,
                    deserialize_entities(entities_raw),
                    has_spoiler,
                    reply_to=msg.message_id,
                    is_doc=is_doc,
                )
                return
            except TelegramError as e:
                logger.error(f"Cover apply error in {chat_id}: {e}")

    # No cover found → ask user
    bot_msg = await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            "🖼️ <b>No cover found for this video.</b>\n\n"
            "Please <b>reply to this message</b> with an image and I'll apply it as the cover!"
        ),
        reply_to_message_id=msg.message_id,
        parse_mode="HTML",
    )
    db_save_pending(
        chat_id, bot_msg.message_id, msg.message_id,
        file_id, caption, entities_raw, has_spoiler, is_doc,
    )

# ══════════════════════════════════════════════
#  HANDLER: VIDEO
# ══════════════════════════════════════════════
async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg or not msg.video:
        return

    video = msg.video

    if msg.chat.type == "channel":
        await _process_channel_video(
            ctx, msg,
            file_id      = video.file_id,
            caption      = msg.caption,
            entities_raw = serialize_entities(msg.caption_entities),
            has_spoiler  = getattr(msg, "has_media_spoiler", False),
            is_doc       = False,
            file_name    = video.file_name or "",
        )
        return

    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        return

    if not await is_joined(uid, ctx):
        await msg.reply_text(
            "🔒 Please join @World_Fastest_Bots first to use this bot.",
            parse_mode="HTML",
        )
        return

    state = db_get_state(uid)
    thumb = state.get("thumbnail")

    if thumb:
        try:
            await send_with_cover(
                ctx, msg.chat_id, video.file_id, thumb,
                msg.caption, msg.caption_entities,
                getattr(msg, "has_media_spoiler", False),
                reply_to=msg.message_id, is_doc=False,
            )
        except TelegramError:
            await msg.reply_text("❌ Error using saved thumbnail. Please send a new photo.")
        return

    db_set_state(uid, {
        "state":        "waiting_for_image",
        "file_id":      video.file_id,
        "caption":      msg.caption,
        "entities":     serialize_entities(msg.caption_entities),
        "has_spoiler":  getattr(msg, "has_media_spoiler", False),
        "is_doc":       False,
        "video_msg_id": msg.message_id,
    })
    await msg.reply_text("✅ Video received! Now send me a photo to use as the cover.")

# ══════════════════════════════════════════════
#  HANDLER: DOCUMENT  (MKV, MP4 as file, etc.)
# ══════════════════════════════════════════════
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg or not msg.document:
        return

    doc = msg.document
    if not is_video_document(doc):
        return

    if msg.chat.type == "channel":
        await _process_channel_video(
            ctx, msg,
            file_id      = doc.file_id,
            caption      = msg.caption,
            entities_raw = serialize_entities(msg.caption_entities),
            has_spoiler  = False,
            is_doc       = True,
            file_name    = doc.file_name or "",
        )
        return

    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        return

    if not await is_joined(uid, ctx):
        await msg.reply_text("🔒 Please join @World_Fastest_Bots first.", parse_mode="HTML")
        return

    state = db_get_state(uid)
    thumb = state.get("thumbnail")

    if thumb:
        try:
            await send_with_cover(
                ctx, msg.chat_id, doc.file_id, thumb,
                msg.caption, msg.caption_entities,
                False, reply_to=msg.message_id, is_doc=True,
            )
        except TelegramError:
            await msg.reply_text("❌ Error applying thumbnail.")
        return

    db_set_state(uid, {
        "state":        "waiting_for_image",
        "file_id":      doc.file_id,
        "caption":      msg.caption,
        "entities":     serialize_entities(msg.caption_entities),
        "has_spoiler":  False,
        "is_doc":       True,
        "video_msg_id": msg.message_id,
    })
    await msg.reply_text("✅ Video file received! Now send me a photo for the cover.")

# ══════════════════════════════════════════════
#  HANDLER: PHOTO
# ══════════════════════════════════════════════
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg or not msg.photo:
        return

    largest = max(msg.photo, key=lambda p: p.file_size)
    chat_id = msg.chat_id

    # Check if replying to a pending-cover bot message (works in channels too)
    if msg.reply_to_message:
        replied_id = msg.reply_to_message.message_id
        pending    = db_get_pending(chat_id, replied_id)

        if pending:
            try:
                await send_with_cover(
                    ctx, chat_id,
                    pending["file_id"],
                    largest.file_id,
                    pending.get("caption"),
                    deserialize_entities(pending.get("entities", [])),
                    pending.get("has_spoiler", False),
                    reply_to=pending["video_msg_id"],
                    is_doc=pending.get("is_doc", False),
                )
                db_del_pending(chat_id, replied_id)
            except TelegramError as e:
                logger.error(f"Pending cover apply error: {e}")
                await msg.reply_text(f"❌ Error applying cover: {e}")
            return

    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        return

    if msg.chat.type != "private" and not await is_joined(uid, ctx):
        await msg.reply_text("🔒 Please join @World_Fastest_Bots first.", parse_mode="HTML")
        return

    state = db_get_state(uid)

    if state.get("state") == "waiting_for_image":
        try:
            await send_with_cover(
                ctx, chat_id,
                state["file_id"],
                largest.file_id,
                state.get("caption"),
                deserialize_entities(state.get("entities", [])),
                state.get("has_spoiler", False),
                reply_to=state.get("video_msg_id"),
                is_doc=state.get("is_doc", False),
            )
        except TelegramError as e:
            await msg.reply_text(f"❌ Error: {e}")

        db_set_state(uid, {
            "thumbnail": largest.file_id,
            "state":     "idle",
            "file_id":   None,
            "caption":   None,
            "entities":  None,
            "is_doc":    False,
        })
        await msg.reply_text(
            "✅ Cover applied and thumbnail saved!\n\nSend another video to use it again.",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]) if msg.chat.type == "private" else None,
        )
    else:
        db_set_state(uid, {**state, "thumbnail": largest.file_id, "state": "idle"})
        await msg.reply_text(
            "✅ <b>Thumbnail saved!</b>\n\nSend me a video to apply it.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([back_to_main_btn()]) if msg.chat.type == "private" else None,
        )

# ══════════════════════════════════════════════
#  STARTUP NOTIFICATION
# ══════════════════════════════════════════════
async def notify_on_start(app: Application) -> None:
    """Send a DM to owner and all admins when bot starts or restarts."""
    notify_ids = set()
    if OWNER_ID:
        notify_ids.add(OWNER_ID)
    notify_ids.update(ADMIN_IDS)

    if not notify_ids:
        return

    text = (
        "🟢 <b>Bot is now ONLINE!</b>\n\n"
        "🎬 Anime Cover Bot started successfully.\n"
        "⚡ @World_Fastest_Bots"
    )
    for uid in notify_ids:
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Startup notify failed for {uid}: {e}")


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set. Set it via the BOT_TOKEN environment variable.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(notify_on_start)
        .build()
    )

    # ── Commands in private/group chats ────────
    for cmd, fn in [
        ("start",      cmd_start),
        ("help",       cmd_help),
        ("cover",      cmd_cover),
        ("allcovers",  cmd_allcovers),
        ("delcover",   cmd_delcover),
        ("listcover",  cmd_listcover),
        ("mythumb",    cmd_mythumb),
        ("delthumb",   cmd_delthumb),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # ── Commands in CHANNELS (channel_post updates) ──
    channel_cmd_filter = filters.ChatType.CHANNEL & filters.COMMAND
    app.add_handler(MessageHandler(
        channel_cmd_filter,
        _dispatch_channel_command,
    ))

    # ── Message handlers ───────────────────────
    app.add_handler(MessageHandler(filters.VIDEO,        handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # ── Callback queries ───────────────────────
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🎬 Anime Cover Bot is running…")
    print("🎬 Anime Cover Bot is running…")

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ──────────────────────────────────────────────
#  CHANNEL COMMAND DISPATCHER
# ──────────────────────────────────────────────
async def _dispatch_channel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    python-telegram-bot's CommandHandler only handles 'message' updates by default.
    This catches channel_post updates that contain commands and dispatches them.
    """
    msg = update.channel_post or update.edited_channel_post
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not text.startswith("/"):
        return

    # Parse command and args
    parts = text.split()
    raw_cmd = parts[0].lstrip("/").split("@")[0].lower()
    ctx.args = parts[1:] if len(parts) > 1 else []

    dispatch_map = {
        "start":     cmd_start,
        "help":      cmd_help,
        "cover":     cmd_cover,
        "allcovers": cmd_allcovers,
        "delcover":  cmd_delcover,
        "listcover": cmd_listcover,
    }

    handler_fn = dispatch_map.get(raw_cmd)
    if handler_fn:
        try:
            await handler_fn(update, ctx)
        except Exception as e:
            logger.error(f"Channel command /{raw_cmd} error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
