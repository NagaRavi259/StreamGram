#Aadhi000
from streamgram.bot import StreamBot
from streamgram.vars import Var
import html
import re
import logging
logger = logging.getLogger("streamgram.bot.plugins.start_help")
from streamgram.bot.plugins.stream import MY_PASS, is_logged_in, login_waiting
from streamgram.utils.human_readable import humanbytes
from streamgram.utils.database import Database
from streamgram.utils.access import has_access, access_db, is_exempt
from streamgram.utils.link_expiry import link_expiry
from pyrogram import Client, filters, StopPropagation, enums
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from streamgram.utils.file_properties import get_media_from_message
from streamgram.utils.force_subscribe import enforce_updates_channel
from streamgram.utils.link_security import link_tokens
from pyrogram.types import ReplyKeyboardMarkup
import asyncio

db = Database.shared(Var.name)


# ----------------- MIDDLEWARE HANDLER (Corrected) ----------------- #

@StreamBot.on_message(filters.private, group=-1)
async def check_user(b: Client, m: Message):
    """
    Middleware. Allowed: owners/trusted users, approved users, members of an access
    group, and people who asked to join one (see streamgram/utils/access.py has_access).
    Everyone else gets a 'Request Access' button; banned users are told they are banned.
    """
    if not Var.USER_GROUP_ID or m.from_user is None:
        logger.debug("check_user: USER_GROUP_ID unset or no from_user, skipping the gate")
        return
    # Invite deep link: /start inv_<token> must work for people who don't have access yet.
    if m.text and m.text.startswith("/start inv_"):
        from streamgram.bot.plugins.access_admin import redeem
        ok = await redeem(b, m.from_user, m.text.split("inv_", 1)[1].strip())
        logger.info("check_user: invite redeem by %s -> %s", m.from_user.id, "ok" if ok else "invalid")
        await m.reply_text(
            "✅ **Invite accepted.** Send me any file to get a link." if ok
            else "❌ This invite is invalid, expired or not for you."
        )
        raise StopPropagation()
    # Let an unapproved user reach the password login flow itself: the /login
    # command, and the password they send right after it.
    if MY_PASS and (m.chat.id in login_waiting or (m.text and (m.text.startswith("/login") or "login🔑" in m.text))):
        logger.debug("check_user: letting %s through to the /login flow", m.from_user.id)
        return
    try:
        allowed, reason = await has_access(b, m.from_user.id)
    except Exception:
        logger.exception("Access check failed")
        await m.reply_text("⚠️ Something went wrong on my side. Please try again in a moment.")
        raise StopPropagation()
    if allowed:
        logger.debug("check_user: %s allowed", m.from_user.id)
        if not is_exempt(m.from_user.id):
            try:
                await access_db.touch(m.from_user)
            except Exception:
                logger.exception("Could not record activity")  # bookkeeping only: never block the user
        return
    # Password login is a standalone access path, independent of admin approval
    # (but a ban still wins). Off by default; only matters once MY_PASS is set.
    if reason != "banned" and await is_logged_in(m.from_user.id):
        logger.info("check_user: %s allowed via password login", m.from_user.id)
        return
    logger.info("check_user: %s denied (%s)", m.from_user.id, reason)
    if reason == "banned":
        await m.reply_text("🚫 **You are banned from this bot.**")
    elif reason == "pending":
        await m.reply_text("⏳ **Your access request is waiting for approval.** You will be notified.")
    else:
        text = (
            "🔒 **Access Denied**\n\nYou are not authorized to use this bot.\n"
            "Tap the button below to ask the admin for access."
        )
        if MY_PASS:
            text += "\n\nOr, if you have the password, send /login."
        await m.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Request Access", callback_data="req:access")]]),
        )
    raise StopPropagation()

# ----------------- END OF MIDDLEWARE ----------------- #

@StreamBot.on_message(filters.command('start') & filters.private)
async def start(b, m):
    logger.debug("start(): from %s", m.from_user.id)
    if not await db.is_user_exist(m.from_user.id):
        logger.info("start(): new user %s", m.from_user.id)
        await db.add_user(m.from_user.id)
        await b.send_message(
            Var.BIN_CHANNEL,
            f"#NEW_USER: \n\nNew User [{m.from_user.first_name}](tg://user?id={m.from_user.id}) Started !!"
        )
    if not await enforce_updates_channel(b, m.chat.id):
        return
    payload = (m.text.split(maxsplit=1) + [""])[1].strip()
    if not payload:
        logger.debug("start(): plain /start, sending the welcome card")
        await m.reply_photo(
            photo="https://telegra.ph/file/3cd15a67ad7234c2945e7.jpg",
            caption="**Hello! ⚡**\n\nI'm StreamGram — send me any file or video and I'll turn it into a permanent download link and a stream link.\n\nUse /help for more details.",
        )
    else:
        # Deep link: /start <message id>_<hash>. The hash is required, so ids cannot be enumerated.
        invalid = "❌ This link is not valid."
        match = re.fullmatch(r"(?:file_)?(\d+)_([A-Za-z0-9_-]{6,12})", payload)
        if not match:
            logger.debug("start(): deep link payload %r does not match the expected pattern", payload)
            await m.reply_text(invalid)
            return
        msg_id, secure_hash = int(match.group(1)), match.group(2)
        media = None
        try:
            get_msg = await b.get_messages(Var.BIN_CHANNEL, msg_id)
            media = None if get_msg.empty else get_media_from_message(get_msg)
        except Exception:
            logger.debug("start(): could not fetch bin-channel message %s", msg_id, exc_info=True)
        if not media:
            logger.info("start(): deep link for message %s has no media", msg_id)
            await m.reply_text(invalid)
            return
        if not await link_tokens.check(msg_id, media.file_unique_id, secure_hash) or await access_db.is_revoked(msg_id) \
                or await link_expiry.is_expired(msg_id):
            logger.info("start(): deep link for message %s rejected (bad hash, revoked or expired)", msg_id)
            await m.reply_text(invalid)
            return
        file_name = html.escape(getattr(media, "file_name", None) or "file")
        file_size = humanbytes(getattr(media, "file_size", 0))
        online_link = f"{Var.URL}{msg_id}/?hash={secure_hash}"
        stream_link = f"{Var.URL}watch/{msg_id}/?hash={secure_hash}"
        logger.info("start(): deep link for message %s delivered to %s", msg_id, m.from_user.id)
        await m.reply_text(
            text=f"<b>Your link is ready ⚡</b>\n\n📧 <b>File name:</b> {file_name}\n📦 <b>Size:</b> {file_size}\n\n💌 <b>Download:</b> {online_link}",
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Watch ⚡", url=stream_link),
                                                InlineKeyboardButton("⚡ Download ⚡", url=online_link)]])
        )


@StreamBot.on_message(filters.command('help') & filters.private)
async def help_handler(bot, message):
    logger.debug("help_handler(): from %s", message.from_user.id)
    if not await db.is_user_exist(message.from_user.id):
        logger.info("help_handler(): new user %s", message.from_user.id)
        await db.add_user(message.from_user.id)
        await bot.send_message(
            Var.BIN_CHANNEL,
            f"#NEW_USER: \n\nNew User [{message.from_user.first_name}](tg://user?id={message.from_user.id}) Started !!"
        )
    if not await enforce_updates_channel(bot, message.chat.id):
        return
    await message.reply_photo(
            photo="https://telegra.ph/file/3cd15a67ad7234c2945e7.jpg",
            caption=(
                "**Send me any file or video and I'll give you a permanent, "
                "shareable link.**\n\n"
                "This link can be used to download the file, or pasted straight "
                "into an external video player to stream it.\n\n"
                "The bot also works in channels — add it as admin to get a "
                "download link posted automatically for every file/video.\n\n"
                "For more info: /about"
            ),
    )

@StreamBot.on_message(filters.command('about') & filters.private)
async def about_handler(bot, message):
    logger.debug("about_handler(): from %s", message.from_user.id)
    if not await db.is_user_exist(message.from_user.id):
        logger.info("about_handler(): new user %s", message.from_user.id)
        await db.add_user(message.from_user.id)
        await bot.send_message(
            Var.BIN_CHANNEL,
            f"#NEW_USER: \n\nNew User [{message.from_user.first_name}](tg://user?id={message.from_user.id}) Started !!"
        )
    if not await enforce_updates_channel(bot, message.chat.id):
        return
    await message.reply_photo(
            photo="https://telegra.ph/file/3cd15a67ad7234c2945e7.jpg",
            caption=(
                "<b>About this bot</b>\n\n"
                "<b>Name:</b> <a href='https://github.com/NagaRavi259/StreamGram'>StreamGram</a>\n"
                "<b>Library:</b> Pyrogram\n"
                "<b>Language:</b> Python 3\n"
                "<b>Source code:</b> <a href='https://github.com/NagaRavi259/StreamGram'>github.com/NagaRavi259/StreamGram</a>"
            ),
    )
