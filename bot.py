import os
import logging
from datetime import datetime

from telegram import Update, MessageEntity, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telegram.error import TelegramError
from pymongo import MongoClient

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN         = ""          # ← your bot token
FORCE_SUB_CHANNEL = -1002432405855
MONGO_URI         = "mongodb+srv://cover:cover0123@cluster0.oilx4yu.mongodb.net/?appName=Cluster0"

# ──────────────────────────────────────────────
#  MONGODB
# ──────────────────────────────────────────────
_client      = MongoClient(MONGO_URI)
_db          = _client["video_cover_bot"]
covers_col   = _db["covers"]        # per-chat anime covers
pending_col  = _db["pending"]       # awaiting cover image in channel
states_col   = _db["user_states"]   # DM user states + personal thumbnail

# indexes (safe to call multiple times)
covers_col.create_index([("chat_id", 1), ("anime_name_lower", 1)], unique=True)
pending_col.create_index([("chat_id", 1), ("bot_msg_id", 1)], unique=True)
states_col.create_index("user_id", unique=True)

# ──────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot_errors.log")]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  SMALL HELPERS
# ──────────────────────────────────────────────
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".3gp")

def get_msg(update: Update):
    """Return whichever message object is present (private/group vs channel)."""
    return update.message or update.channel_post

def is_video_document(doc) -> bool:
    if doc is None:
        return False
    mime = doc.mime_type or ""
    name = (doc.file_name or "").lower()
    return mime.startswith("video/") or name.endswith(VIDEO_EXTENSIONS)

def serialize_entities(entities) -> list:
    if not entities:
        return []
    out = []
    for e in entities:
        out.append({
            "type": e.type,
            "offset": e.offset,
            "length": e.length,
            "user": e.user.to_dict() if e.user else None,
        })
    return out

def deserialize_entities(data: list):
    if not data:
        return None
    out = []
    for e in data:
        out.append(MessageEntity(
            type=e["type"],
            offset=e["offset"],
            length=e["length"],
            user=e.get("user"),
        ))
    return out or None

# ──────────────────────────────────────────────
#  DB HELPERS  – covers
# ──────────────────────────────────────────────
def db_save_cover(chat_id: int, chat_name: str, anime_name: str, file_id: str):
    covers_col.update_one(
        {"chat_id": chat_id, "anime_name_lower": anime_name.strip().lower()},
        {"$set": {
            "chat_id":          chat_id,
            "chat_name":        chat_name,
            "anime_name":       anime_name.strip(),
            "anime_name_lower": anime_name.strip().lower(),
            "file_id":          file_id,
            "updated_at":       datetime.utcnow(),
        }},
        upsert=True,
    )

def db_get_cover(chat_id: int, anime_name: str):
    return covers_col.find_one({"chat_id": chat_id, "anime_name_lower": anime_name.strip().lower()})

def db_all_covers(chat_id: int) -> list:
    return list(covers_col.find({"chat_id": chat_id}).sort("anime_name", 1))

def db_del_cover_name(chat_id: int, anime_name: str) -> bool:
    r = covers_col.delete_one({"chat_id": chat_id, "anime_name_lower": anime_name.strip().lower()})
    return r.deleted_count > 0

def db_del_cover_index(chat_id: int, idx: int):
    """Delete by 1-based index from sorted list. Returns deleted name or None."""
    covers = db_all_covers(chat_id)
    if 1 <= idx <= len(covers):
        doc = covers[idx - 1]
        covers_col.delete_one({"_id": doc["_id"]})
        return doc["anime_name"]
    return None

def db_channels_with_covers() -> list:
    """Aggregate: list of {_id: chat_id, chat_name, count}."""
    pipeline = [
        {"$group": {"_id": "$chat_id",
                    "chat_name": {"$first": "$chat_name"},
                    "count":     {"$sum":  1}}},
        {"$sort": {"chat_name": 1}},
    ]
    return list(covers_col.aggregate(pipeline))

# ──────────────────────────────────────────────
#  DB HELPERS  – pending (channel awaiting cover)
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
#  DB HELPERS  – user states (DM thumbnail etc.)
# ──────────────────────────────────────────────
def db_get_state(user_id: int) -> dict:
    return states_col.find_one({"user_id": user_id}) or {}

def db_set_state(user_id: int, patch: dict):
    states_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id, **patch}}, upsert=True)

# ──────────────────────────────────────────────
#  ANIME NAME DETECTION
# ──────────────────────────────────────────────
def find_anime_in_text(text: str, known: list) -> str | None:
    if not text:
        return None
    text_l = text.lower()
    for name in known:
        if name.lower() in text_l:
            return name
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
async def chat_name(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
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
    """Send video or document with a cover/thumbnail, replying to reply_to."""
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
        await ctx.bot.send_video(
            chat_id=chat_id,
            video=file_id,
            cover=cover_file_id,
            caption=caption,
            caption_entities=entities,
            supports_streaming=True,
            has_spoiler=has_spoiler,
            reply_to_message_id=reply_to,
        )

# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    await msg.reply_text(
        "🎬 <b>Video Cover Bot</b>\n\n"
        "📺 <b>Channel Features:</b>\n"
        "• /cover [animename] — reply to image to save a cover\n"
        "• Send any video/MKV — bot auto-applies cover by anime name\n"
        "• If no cover found — bot asks you to send one\n\n"
        "💬 <b>DM Features:</b>\n"
        "• Send a photo → saved as your personal thumbnail\n"
        "• Send any video → thumbnail applied automatically\n\n"
        "🛠 <b>Commands:</b>\n"
        "/cover [name] — Save anime cover (reply to image)\n"
        "/allcovers — List covers in this chat\n"
        "/delcover [name or number] — Delete a cover\n"
        "/listcover — Browse all chats with covers\n"
        "/mythumb — See your personal thumbnail\n"
        "/delthumb — Remove your personal thumbnail\n"
        "/help — Full help guide\n\n"
        "⚡ <b>Powered by:</b> @World_Fastest_Bots",
        parse_mode="HTML",
    )

# ══════════════════════════════════════════════
#  /help
# ══════════════════════════════════════════════
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    await msg.reply_text(
        "🎬 <b>Video Cover Bot – Help</b>\n\n"
        "<b>Channel workflow:</b>\n"
        "1. Reply to any image with /cover [animename]\n"
        "2. When a video is posted, bot checks its filename & caption\n"
        "3. If anime name matches → cover auto-applied\n"
        "4. If no match → bot asks for a cover image\n"
        "5. Reply to that message with an image → cover applied\n\n"
        "<b>DM workflow:</b>\n"
        "1. Send a photo → set as your thumbnail\n"
        "2. Send any video → thumbnail applied instantly\n\n"
        "<b>All commands:</b>\n"
        "/cover [name] — Save cover (channel or DM)\n"
        "/allcovers — Show covers for this chat\n"
        "/delcover [name or number] — Delete cover\n"
        "/listcover — List all chats with covers (paginated)\n"
        "/mythumb — View your thumbnail\n"
        "/delthumb — Delete your thumbnail\n\n"
        "⚡ <b>Powered by:</b> @World_Fastest_Bots",
        parse_mode="HTML",
    )

# ══════════════════════════════════════════════
#  /cover  [reply to image]
# ══════════════════════════════════════════════
async def cmd_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not msg:
        return

    if not ctx.args:
        await msg.reply_text("❌ Usage: /cover [animename]\nReply to an image with this command.")
        return

    anime = " ".join(ctx.args).strip()

    if not msg.reply_to_message:
        await msg.reply_text("❌ Please <b>reply to an image</b> with this command.", parse_mode="HTML")
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
        await msg.reply_text("❌ The replied message must be an image, photo, or sticker.")
        return

    cname = await chat_name(msg.chat_id, ctx)
    db_save_cover(msg.chat_id, cname, anime, file_id)

    await msg.reply_text(
        f"✅ <b>Cover saved!</b>\n\n"
        f"🎌 Anime: <b>{anime}</b>\n"
        f"📺 Chat: <b>{cname}</b>\n\n"
        f"This cover will be applied automatically when a video with "
        f"<b>{anime}</b> in its name or caption is posted here.",
        parse_mode="HTML",
    )

# ══════════════════════════════════════════════
#  /allcovers
# ══════════════════════════════════════════════
async def cmd_allcovers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    cid = msg.chat_id
    cname = await chat_name(cid, ctx)
    covers = db_all_covers(cid)

    if not covers:
        await msg.reply_text(f"❌ No covers saved for <b>{cname}</b> yet.\nUse /cover [animename] (reply to image) to add one.", parse_mode="HTML")
        return

    lines = "\n".join(f"{i}. {c['anime_name']}" for i, c in enumerate(covers, 1))
    await msg.reply_text(
        f"🎌 <b>Covers in {cname}</b>\n\n{lines}\n\n📊 Total: {len(covers)}",
        parse_mode="HTML",
    )

# ══════════════════════════════════════════════
#  /delcover  [name or number]
# ══════════════════════════════════════════════
async def cmd_delcover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
    if not ctx.args:
        await msg.reply_text("❌ Usage: /delcover [animename] or /delcover [number]")
        return

    arg = " ".join(ctx.args).strip()
    cid = msg.chat_id

    if arg.isdigit():
        deleted = db_del_cover_index(cid, int(arg))
        if deleted:
            await msg.reply_text(f"✅ Deleted cover <b>#{arg}</b>: <b>{deleted}</b>", parse_mode="HTML")
        else:
            await msg.reply_text("❌ Invalid number. Use /allcovers to see the list.")
    else:
        ok = db_del_cover_name(cid, arg)
        if ok:
            await msg.reply_text(f"✅ Deleted cover: <b>{arg}</b>", parse_mode="HTML")
        else:
            await msg.reply_text(f"❌ No cover found with name: <b>{arg}</b>", parse_mode="HTML")

# ══════════════════════════════════════════════
#  /listcover  (paginated channel list)
# ══════════════════════════════════════════════
CHANS_PER_PAGE = 8

def _build_channel_list_keyboard(channels: list, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total      = len(channels)
    total_pages = max(1, (total + CHANS_PER_PAGE - 1) // CHANS_PER_PAGE)
    start      = page * CHANS_PER_PAGE
    page_items = channels[start: start + CHANS_PER_PAGE]

    rows   = []
    pair   = []
    for ch in page_items:
        label = f"📺 {ch.get('chat_name', ch['_id'])} ({ch['count']})"
        pair.append(InlineKeyboardButton(label, callback_data=f"ch:{ch['_id']}:0"))
        if len(pair) == 2:
            rows.append(pair); pair = []
    if pair:
        rows.append(pair)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cl:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cl:{page+1}"))
    if nav:
        rows.append(nav)

    text = (
        f"📋 <b>Chats with saved covers</b>\n\n"
        f"Page {page+1}/{total_pages}  •  {total} chat(s)"
    )
    return text, InlineKeyboardMarkup(rows)


async def cmd_listcover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
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
    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        await msg.reply_text("❌ This command is only for personal DMs.")
        return
    thumb = db_get_state(uid).get("thumbnail")
    if thumb:
        try:
            await msg.reply_photo(
                photo=thumb,
                caption="🖼️ <b>Your current thumbnail</b>\nUse /delthumb to remove it.",
                parse_mode="HTML",
            )
        except Exception:
            await msg.reply_text("❌ Can't load thumbnail. Please set a new one.")
    else:
        await msg.reply_text("❌ No thumbnail saved. Send me a photo to set one.")

async def cmd_delthumb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = get_msg(update)
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
    data = q.data

    # ── channel list page ──────────────────────
    if data.startswith("cl:"):
        page     = int(data[3:])
        channels = db_channels_with_covers()
        text, kb = _build_channel_list_keyboard(channels, page)
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    # ── single channel covers ──────────────────
    elif data.startswith("ch:"):
        _, cid_s, page_s = data.split(":")
        cid  = int(cid_s)
        page = int(page_s)
        covers = db_all_covers(cid)

        per_page    = 10
        total       = len(covers)
        total_pages = max(1, (total + per_page - 1) // per_page)
        start       = page * per_page
        chunk       = covers[start: start + per_page]
        cname       = covers[0]["chat_name"] if covers else str(cid)

        lines = "\n".join(f"{start+i+1}. {c['anime_name']}" for i, c in enumerate(chunk))
        text  = (
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

        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([nav]), parse_mode="HTML")

    # ── force-sub check ────────────────────────
    elif data == "check_join":
        uid = q.from_user.id
        if await is_joined(uid, ctx):
            await q.edit_message_text(
                "✅ <b>Welcome!</b>\n\nYou can now use the bot.\n\n⚡ @World_Fastest_Bots",
                parse_mode="HTML",
            )
        else:
            await q.answer("Please join the channel first!", show_alert=True)

# ══════════════════════════════════════════════
#  CORE: process a video/document posted in channel
# ══════════════════════════════════════════════
async def _process_channel_video(ctx, msg, file_id, caption, entities_raw,
                                  has_spoiler, is_doc, file_name=""):
    chat_id = msg.chat_id

    # Build search texts
    search_texts = [t for t in [caption, file_name] if t]

    covers = db_all_covers(chat_id)
    found_anime = None

    if covers:
        known = [c["anime_name"] for c in covers]
        for txt in search_texts:
            found_anime = find_anime_in_text(txt, known)
            if found_anime:
                break

    if found_anime:
        cover_doc = db_get_cover(chat_id, found_anime)
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
                logger.error(f"Cover apply error: {e}")

    # ── no cover found → ask user ──────────────
    bot_msg = await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            "🖼️ <b>No cover found for this video.</b>\n\n"
            "Please <b>reply to this message</b> with an image and I will apply it as the cover!"
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

    # ── channel ────────────────────────────────
    if msg.chat.type == "channel":
        await _process_channel_video(
            ctx, msg,
            file_id       = video.file_id,
            caption       = msg.caption,
            entities_raw  = serialize_entities(msg.caption_entities),
            has_spoiler   = getattr(msg, "has_media_spoiler", False),
            is_doc        = False,
            file_name     = video.file_name or "",
        )
        return

    # ── DM / group ─────────────────────────────
    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        return

    if not await is_joined(uid, ctx):
        await msg.reply_text("🔒 Please join @World_Fastest_Bots first to use this bot.", parse_mode="HTML")
        return

    state = db_get_state(uid)
    thumb = state.get("thumbnail")

    if thumb:
        try:
            await send_with_cover(
                ctx, msg.chat_id, video.file_id, thumb,
                msg.caption, msg.caption_entities,
                getattr(msg, "has_media_spoiler", False),
                reply_to=msg.message_id,
                is_doc=False,
            )
        except TelegramError:
            await msg.reply_text("❌ Error using saved thumbnail. Please set a new one.")
        return

    # No thumbnail yet → save state, ask for photo
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
        return   # not a video file – ignore

    # ── channel ────────────────────────────────
    if msg.chat.type == "channel":
        await _process_channel_video(
            ctx, msg,
            file_id       = doc.file_id,
            caption       = msg.caption,
            entities_raw  = serialize_entities(msg.caption_entities),
            has_spoiler   = False,
            is_doc        = True,
            file_name     = doc.file_name or "",
        )
        return

    # ── DM / group ─────────────────────────────
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

    largest  = max(msg.photo, key=lambda p: p.file_size)
    chat_id  = msg.chat_id

    # ── Check if replying to a pending-cover bot message ──
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
                await msg.reply_text(f"❌ Error: {e}")
            return   # handled

    # ── DM / group user state ──────────────────
    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        return

    # Skip force-sub check for DM photo (bot may be getting a photo as first message)
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

        # Save as thumbnail and reset state
        db_set_state(uid, {
            "thumbnail": largest.file_id,
            "state":     "idle",
            "file_id":   None,
            "caption":   None,
            "entities":  None,
            "is_doc":    False,
        })
    else:
        # Just save as personal thumbnail
        db_set_state(uid, {**state, "thumbnail": largest.file_id, "state": "idle"})
        await msg.reply_text("✅ Thumbnail saved! Send me a video to apply it.")

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("cover",      cmd_cover))
    app.add_handler(CommandHandler("allcovers",  cmd_allcovers))
    app.add_handler(CommandHandler("delcover",   cmd_delcover))
    app.add_handler(CommandHandler("listcover",  cmd_listcover))
    app.add_handler(CommandHandler("mythumb",    cmd_mythumb))
    app.add_handler(CommandHandler("delthumb",   cmd_delthumb))

    # Message handlers (order matters – specific before general)
    app.add_handler(MessageHandler(filters.VIDEO,        handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("🎬 Video Cover Bot is running…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,   # ← required to receive channel posts
    )

if __name__ == "__main__":
    main()
