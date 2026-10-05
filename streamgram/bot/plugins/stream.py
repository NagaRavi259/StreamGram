#(c) Adarsh-Goel
import hashlib
import os
import re
import time
import asyncio
from asyncio import TimeoutError
from streamgram.bot import StreamBot
from streamgram.utils.database import Database
from streamgram.utils.human_readable import humanbytes
from streamgram.vars import Var
from streamgram.utils.access import access_db, channel_sponsor, fmt_duration
from streamgram.utils.link_expiry import link_expiry
from urllib.parse import quote_plus
from pyrogram import filters, Client
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from streamgram.utils.file_properties import get_name, get_hash, get_media_file_size
import logging
logger = logging.getLogger("streamgram.bot.plugins.stream")

db = Database.shared(Var.name)


MY_PASS = os.environ.get("MY_PASS",None)


def pass_token(password):
    """What is stored for a logged-in user: a hash, never the password. Changing MY_PASS logs everyone out."""
    return hashlib.sha256(password.encode()).hexdigest()

pass_dict = {}
pass_db = Database.shared("ag_passwords")


async def is_logged_in(user_id) -> bool:
    """Password login is a standalone access path: logging in grants use of the
    bot on its own, independent of the admin-approval / access-group system."""
    if not MY_PASS:
        return False
    return await pass_db.get_user_pass(user_id) == pass_token(MY_PASS)


login_waiting = {}  # chat_id -> time until which the next text message is treated as the password


@StreamBot.on_message(filters.private & (filters.regex("login🔑") | filters.command("login")), group=4)
async def login_handler(c: Client, m: Message):
    if not MY_PASS:
        await m.reply_text("Password login is not enabled.")
        return
    login_waiting[m.chat.id] = time.time() + 90
    await m.reply_text("Now send me the password.\n\n(You can use /cancel to cancel; I wait 90 seconds.)")


@StreamBot.on_message(filters.private & filters.text, group=3)
async def login_password_handler(c: Client, m: Message):
    """Receives the password after /login (replaces the pyromod `listen` call that was never installed)."""
    expiry = login_waiting.pop(m.chat.id, None)
    if expiry is None:
        return
    text = m.text.strip()
    if text.startswith("/") and text != "/cancel":
        return  # another command: abandon the login and let it run
    if text == "/cancel":
        await m.reply_text("Process Cancelled Successfully")
    elif time.time() > expiry:
        await m.reply_text("I can't wait more for the password, try /login again")
    elif text == MY_PASS:
        await pass_db.add_user_pass(m.chat.id, pass_token(text))
        await m.reply_text("yeah! you entered the password correctly")
    else:
        await m.reply_text("Wrong password, try again")
    try:
        await m.delete()  # don't leave the password in the chat
    except Exception:
        pass


@StreamBot.on_message((filters.private) & (filters.document | filters.video | filters.audio | filters.photo) , group=4)
async def private_receive_handler(c: Client, m: Message):
    # Access (admin-approved OR password-logged-in) was already decided by
    # start_help.check_user before this handler runs; no separate gate here.
    if not await db.is_user_exist(m.from_user.id):
        await db.add_user(m.from_user.id)
        await c.send_message(
            Var.BIN_CHANNEL,
            f"Nᴇᴡ Usᴇʀ Jᴏɪɴᴇᴅ : \n\n Nᴀᴍᴇ : [{m.from_user.first_name}](tg://user?id={m.from_user.id}) Sᴛᴀʀᴛᴇᴅ Yᴏᴜʀ Bᴏᴛ !!"
        )
    if Var.UPDATES_CHANNEL:
        try:
            user = await c.get_chat_member(Var.UPDATES_CHANNEL, m.chat.id)
            if user.status == "kicked":
                await c.send_message(
                    chat_id=m.chat.id,
                    text="𝚈𝙾𝚄 𝙰𝚁𝙴 𝙱𝙰𝙽𝙽𝙴𝙳../**",
                    disable_web_page_preview=True
                )
                return
        except UserNotParticipant:
            await c.send_message(
                chat_id=m.chat.id,
                text="""<i>ᴊᴏɪɴ ᴍʏ ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴜsᴇ ᴍᴇ..**</i>""",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("ᴊᴏɪɴ ɴᴏᴡ", url=f"https://t.me/{Var.UPDATES_CHANNEL}")
                        ]
                    ]
                )
            )
            return
        except Exception as e:
            await m.reply_text(e)
            await c.send_message(
                chat_id=m.chat.id,
                text='\n🎉 Welcome to the Ultimate Test Bot! 🎉**\n\n🔹 **Enjoy All Features for FREE!**\n🔹 **No Ads, No Subscription!**\n\n**📁 How to Use:**\n\n1. **Forward a File** to this bot.\n2. **Receive a Link** to **Stream** or **Download** your file instantly!\n\n**💡 Key Features:**\n\n- **Completely Ad-Free Experience** 🚫\n- **No Subscription Required** 🎟️\n- **Fast & Easy File Sharing** 📤',
                disable_web_page_preview=True)
            return
    # Check the limit and count this link in one step (a burst of files cannot all slip through).
    try:
        allowed, limit_msg, reservation = await access_db.reserve(m.from_user.id)
    except Exception:
        logger.exception("Could not check the link limit")
        await m.reply_text("⚠️ Something went wrong on my side. Please try again in a moment.", quote=True)
        return
    if not allowed:
        await m.reply_text(limit_msg, quote=True)
        return
    try:
        log_msg = await m.forward(chat_id=Var.BIN_CHANNEL)
        expires_at = await link_expiry.register(log_msg.id, m.from_user.id)
        # stream_link = f"{Var.URL}watch/{str(log_msg.id)}/{quote_plus(get_name(log_msg))}?hash={get_hash(log_msg)}"

        # online_link = f"{Var.URL}{str(log_msg.id)}/{quote_plus(get_name(log_msg))}?hash={get_hash(log_msg)}"

        stream_link = f"{Var.URL}watch/{str(log_msg.id)}/?hash={get_hash(log_msg)}"

        online_link = f"{Var.URL}{str(log_msg.id)}/?hash={get_hash(log_msg)}"

        photo_xr="https://telegra.ph/file/3cd15a67ad7234c2945e7.jpg"



        msg_text ="""
<b>Your link is ready ⚡</b>

<b>📧 File name:</b> <i><b>{}</b></i>

<b>📦 File size:</b> <i><b>{}</b></i>

<b>💌 Download link:</b> <i><b>{}</b></i>

<b>🖥 Watch online:</b> <i><b>{}</b></i>

<b>♻️ This link is permanent and won't expire ♻️</b>"""

        await log_msg.reply_text(text=f"**RᴇQᴜᴇꜱᴛᴇᴅ ʙʏ :** [{m.from_user.first_name}](tg://user?id={m.from_user.id})\n**Uꜱᴇʀ ɪᴅ :** `{m.from_user.id}`\n**Stream ʟɪɴᴋ :** {stream_link}", disable_web_page_preview=True, quote=True)
        link_text = msg_text.format(get_name(log_msg), humanbytes(get_media_file_size(m)), online_link, stream_link)
        if expires_at:  # replace the 'permanent' note
            link_text = re.sub(r"♻️[^♻]*♻️", f"⏳ This link expires in {fmt_duration(round(expires_at - time.time()))} ⏳", link_text, count=1)
        await m.reply_text(

            text=link_text,

            quote=True,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ ᴡᴀᴛᴄʜ ⚡", url=stream_link), #Stream Link
                                                InlineKeyboardButton('⚡ ᴅᴏᴡɴʟᴏᴀᴅ ⚡', url=online_link)]]) #Download Link
        )
    except FloodWait as e:
        await access_db.refund(reservation)  # no link was delivered, so it must not count
        logger.warning(f"FloodWait: sleeping for {e.value}s")
        await asyncio.sleep(e.value)
        await c.send_message(chat_id=Var.BIN_CHANNEL, text=f"Gᴏᴛ FʟᴏᴏᴅWᴀɪᴛ ᴏғ {str(e.value)}s from [{m.from_user.first_name}](tg://user?id={m.from_user.id})\n\n**𝚄𝚜𝚎𝚛 𝙸𝙳 :** `{str(m.from_user.id)}`", disable_web_page_preview=True)
        await m.reply_text("Telegram asked me to slow down. Please send the file again.", quote=True)
    except Exception:
        await access_db.refund(reservation)
        raise


@StreamBot.on_message(filters.channel & ~filters.group & (filters.document | filters.video | filters.photo) & ~filters.forwarded, group=-1)
async def channel_receive_handler(bot, broadcast):
    # Access groups/channels are only used to check membership: never reply to, forward or edit anything there.
    if broadcast.chat.id in await access_db.group_ids():
        return
    if int(broadcast.chat.id) in Var.BANNED_CHANNELS:
        await bot.leave_chat(broadcast.chat.id)
        return
    # Only channels with an admin who has access to the bot are served (and that admin's limit applies).
    sponsor = await channel_sponsor(bot, broadcast.chat.id)
    if sponsor is None:
        logger.info(f"Ignoring channel {broadcast.chat.id}: none of its admins has access to the bot")
        return
    try:
        allowed, limit_msg, reservation = await access_db.reserve(sponsor)
    except Exception:
        logger.exception("Could not check the link limit for a channel post")
        return
    if not allowed:
        logger.info(f"Channel {broadcast.chat.id} skipped, limit reached for admin {sponsor}")
        return
    try:
        log_msg = await broadcast.forward(chat_id=Var.BIN_CHANNEL)
        await link_expiry.register(log_msg.id, sponsor)
        stream_link = f"{Var.URL}watch/{str(log_msg.id)}/{quote_plus(get_name(log_msg))}?hash={get_hash(log_msg)}"
        online_link = f"{Var.URL}{str(log_msg.id)}/{quote_plus(get_name(log_msg))}?hash={get_hash(log_msg)}"
        await log_msg.reply_text(
            text=f"**Cʜᴀɴɴᴇʟ Nᴀᴍᴇ:** `{broadcast.chat.title}`\n**Cʜᴀɴɴᴇʟ ID:** `{broadcast.chat.id}`\n**Rᴇǫᴜᴇsᴛ ᴜʀʟ:** {stream_link}",
            quote=True
        )
        await bot.edit_message_reply_markup(
            chat_id=broadcast.chat.id,
            id=broadcast.id,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚡ ᴡᴀᴛᴄʜ ⚡", url=stream_link),
                     InlineKeyboardButton('⚡ ᴅᴏᴡɴʟᴏᴀᴅ ⚡', url=online_link)]
                ]
            )
        )
    except FloodWait as w:
        await access_db.refund(reservation)
        logger.warning(f"FloodWait: sleeping for {w.value}s")
        await asyncio.sleep(w.value)
        await bot.send_message(chat_id=Var.BIN_CHANNEL,
                             text=f"Gᴏᴛ FʟᴏᴏᴅWᴀɪᴛ ᴏғ {str(w.value)}s from {broadcast.chat.title}\n\n**Cʜᴀɴɴᴇʟ ID:** `{str(broadcast.chat.id)}`",
                             disable_web_page_preview=True)
    except Exception as e:
        await access_db.refund(reservation)
        await bot.send_message(chat_id=Var.BIN_CHANNEL, text=f"**#ᴇʀʀᴏʀ_ᴛʀᴀᴄᴇʙᴀᴄᴋ:** `{e}`", disable_web_page_preview=True)
        logger.error(f"Cannot edit the channel message, give the bot edit permission in the updates and bin channels: {e}")
