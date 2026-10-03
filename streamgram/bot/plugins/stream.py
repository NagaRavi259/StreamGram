#(c) Adarsh-Goel
import os
import time
import asyncio
from asyncio import TimeoutError
from streamgram.bot import StreamBot
from streamgram.utils.database import Database, get_mongo_uri
from streamgram.utils.human_readable import humanbytes
from streamgram.vars import Var
from streamgram.utils.access import access_db, channel_sponsor
from urllib.parse import quote_plus
from pyrogram import filters, Client
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from streamgram.utils.file_properties import get_name, get_hash, get_media_file_size
import logging
import sys

try:
    loop = asyncio.get_event_loop()
    dabase_url = get_mongo_uri()
    db = Database(dabase_url, Var.name)
    if loop.is_running():
        # If the event loop is already running, schedule the task in it
        task = loop.create_task(db.initialize())
        # Optionally, handle exceptions inside the task
        task.add_done_callback(
            lambda t: t.exception() and logging.critical(f"Database initialization error: {t.exception()}")
        )
except Exception as e:
    logging.critical(f"Critical error occurred during database initialization: {e}")
    sys.exit(1)  # Force exit the program if database initialization fails


MY_PASS = os.environ.get("MY_PASS",None)
pass_dict = {}
try:
    loop = asyncio.get_event_loop()
    dabase_url = get_mongo_uri()
    pass_db = Database(dabase_url, "ag_passwords")
    if loop.is_running():
        # If the event loop is already running, schedule the task in it
        task = loop.create_task(pass_db.initialize())
        # Optionally, handle exceptions inside the task
        task.add_done_callback(
            lambda t: t.exception() and logging.critical(f"Database initialization error: {t.exception()}")
        )
except Exception as e:
    logging.critical(f"Critical error occurred during database initialization: {e}")
    sys.exit(1)  # Force exit the program if database initialization fails


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
        await pass_db.add_user_pass(m.chat.id, text)
        await m.reply_text("yeah! you entered the password correctly")
    else:
        await m.reply_text("Wrong password, try again")
    try:
        await m.delete()  # don't leave the password in the chat
    except Exception:
        pass


@StreamBot.on_message((filters.private) & (filters.document | filters.video | filters.audio | filters.photo) , group=4)
async def private_receive_handler(c: Client, m: Message):
    if MY_PASS:
        check_pass = await pass_db.get_user_pass(m.chat.id)
        if check_pass== None:
            await m.reply_text("Login first using /login cmd \nDon't know the password contact @ArjunVR_AVR")
            return
        if check_pass != MY_PASS:
            await pass_db.delete_user(m.chat.id)
            return
    if not await db.is_user_exist(m.from_user.id):
        await db.add_user(m.from_user.id)
        await c.send_message(
            Var.BIN_CHANNEL,
            f"Nᴇᴡ Usᴇʀ Jᴏɪɴᴇᴅ : \n\n Nᴀᴍᴇ : [{m.from_user.first_name}](tg://user?id={m.from_user.id}) Sᴛᴀʀᴛᴇᴅ Yᴏᴜʀ Bᴏᴛ !!"
        )
    if Var.UPDATES_CHANNEL != "None":
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
    try:

        allowed, limit_msg = await access_db.check_quota(m.from_user.id)
        if not allowed:
            await m.reply_text(limit_msg, quote=True)
            return
        log_msg = await m.forward(chat_id=Var.BIN_CHANNEL)
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
        await m.reply_text(

            text=msg_text.format(get_name(log_msg), humanbytes(get_media_file_size(m)), online_link, stream_link),

            quote=True,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ ᴡᴀᴛᴄʜ ⚡", url=stream_link), #Stream Link
                                                InlineKeyboardButton('⚡ ᴅᴏᴡɴʟᴏᴀᴅ ⚡', url=online_link)]]) #Download Link
        )
        await access_db.record_usage(m.from_user.id)
    except FloodWait as e:
        print(f"Sleeping for {str(e.x)}s")
        await asyncio.sleep(e.x)
        await c.send_message(chat_id=Var.BIN_CHANNEL, text=f"Gᴏᴛ FʟᴏᴏᴅWᴀɪᴛ ᴏғ {str(e.x)}s from [{m.from_user.first_name}](tg://user?id={m.from_user.id})\n\n**𝚄𝚜𝚎𝚛 𝙸𝙳 :** `{str(m.from_user.id)}`", disable_web_page_preview=True)


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
        logging.info(f"Ignoring channel {broadcast.chat.id}: none of its admins has access to the bot")
        return
    allowed, limit_msg = await access_db.check_quota(sponsor)
    if not allowed:
        logging.info(f"Channel {broadcast.chat.id} skipped, limit reached for admin {sponsor}")
        return
    if MY_PASS:
        check_pass = await pass_db.get_user_pass(broadcast.chat.id)
        if check_pass == None:
            await broadcast.reply_text("Login first using /login cmd \n don\'t know the pass? ask the bot owner")
            return
        if check_pass != MY_PASS:
            await broadcast.reply_text("Wrong password, login again")
            await pass_db.delete_user(broadcast.chat.id)
            return
    try:
        log_msg = await broadcast.forward(chat_id=Var.BIN_CHANNEL)
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
        await access_db.record_usage(sponsor)
    except FloodWait as w:
        print(f"Sleeping for {str(w.x)}s")
        await asyncio.sleep(w.x)
        await bot.send_message(chat_id=Var.BIN_CHANNEL,
                             text=f"Gᴏᴛ FʟᴏᴏᴅWᴀɪᴛ ᴏғ {str(w.x)}s from {broadcast.chat.title}\n\n**Cʜᴀɴɴᴇʟ ID:** `{str(broadcast.chat.id)}`",
                             disable_web_page_preview=True)
    except Exception as e:
        await bot.send_message(chat_id=Var.BIN_CHANNEL, text=f"**#ᴇʀʀᴏʀ_ᴛʀᴀᴄᴇʙᴀᴄᴋ:** `{e}`", disable_web_page_preview=True)
        print(f"Cᴀɴ'ᴛ Eᴅɪᴛ Bʀᴏᴀᴅᴄᴀsᴛ Mᴇssᴀɢᴇ!\nEʀʀᴏʀ:  **Give me edit permission in updates and bin Chanell{e}**")
