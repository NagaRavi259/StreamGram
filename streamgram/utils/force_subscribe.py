"""Shared 'must be a member of Var.UPDATES_CHANNEL' check, used by /start, /help, /about and the
private file handler. A no-op when UPDATES_CHANNEL isn't set."""
import logging
logger = logging.getLogger("streamgram.utils.force_subscribe")

from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from streamgram.vars import Var

WELCOME_TEXT = (
    '\n🎉 Welcome to the Ultimate Test Bot! 🎉**\n\n🔹 **Enjoy All Features for FREE!**\n'
    '🔹 **No Ads, No Subscription!**\n\n**📁 How to Use:**\n\n1. **Forward a File** to this bot.\n'
    '2. **Receive a Link** to **Stream** or **Download** your file instantly!\n\n'
    '**💡 Key Features:**\n\n- **Completely Ad-Free Experience** 🚫\n- **No Subscription Required** 🎟️\n'
    '- **Fast & Easy File Sharing** 📤'
)


async def enforce_updates_channel(client, chat_id) -> bool:
    """True if the caller may proceed. Otherwise this already replied to chat_id and the
    caller should stop (banned from, or not yet a member of, Var.UPDATES_CHANNEL)."""
    if not Var.UPDATES_CHANNEL:
        logger.debug("enforce_updates_channel(%s): UPDATES_CHANNEL unset, nothing to enforce", chat_id)
        return True
    try:
        member = await client.get_chat_member(Var.UPDATES_CHANNEL, chat_id)
        if member.status == "banned":
            logger.info("enforce_updates_channel(%s): banned from %s", chat_id, Var.UPDATES_CHANNEL)
            await client.send_message(chat_id=chat_id, text="**ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ../**", disable_web_page_preview=True)
            return False
    except UserNotParticipant:
        logger.debug("enforce_updates_channel(%s): not a member of %s yet", chat_id, Var.UPDATES_CHANNEL)
        await client.send_message(
            chat_id=chat_id,
            text="**ᴊᴏɪɴ ᴍʏ ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴜsᴇ  ᴍᴇ..**\n\n"
                 "**ᴅᴜᴇ ᴛᴏ ᴏᴠᴇʀʟᴏᴀᴅ ᴏɴʟʏ ᴄʜᴀɴɴᴇʟ sᴜʙsᴄʀɪʙᴇʀs ᴄᴀɴ ᴜsᴇ ᴍᴇ..!**",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("ᴊᴏɪɴ ᴍʏ ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ", url=f"https://t.me/{Var.UPDATES_CHANNEL}")]]
            ),
        )
        return False
    except Exception:
        logger.warning("get_chat_member(%s, %s) failed", Var.UPDATES_CHANNEL, chat_id, exc_info=True)
        await client.send_message(chat_id=chat_id, text=WELCOME_TEXT, disable_web_page_preview=True)
        return False
    logger.debug("enforce_updates_channel(%s): member of %s, proceeding", chat_id, Var.UPDATES_CHANNEL)
    return True
