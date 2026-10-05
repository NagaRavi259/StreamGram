# (c) adarsh-goel

import asyncio
import logging
import traceback
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, PeerIdInvalid

logger = logging.getLogger("streamgram.utils.broadcast_helper")


async def send_msg(user_id, message):
    try:
        await message.forward(chat_id=user_id)
        logger.debug("send_msg(%s): delivered", user_id)
        return 200, None
    except FloodWait as e:
        logger.warning("send_msg(%s): FloodWait %ss, retrying after sleeping", user_id, e.value)
        await asyncio.sleep(e.value)
        return await send_msg(user_id, message)
    except InputUserDeactivated:
        logger.info("send_msg(%s): account deactivated", user_id)
        return 400, f"{user_id} : deactivated\n"
    except UserIsBlocked:
        logger.info("send_msg(%s): user has blocked the bot", user_id)
        return 400, f"{user_id} : blocked the bot\n"
    except PeerIdInvalid:
        logger.warning("send_msg(%s): invalid user id", user_id)
        return 400, f"{user_id} : user id invalid\n"
    except Exception as e:
        logger.error("send_msg(%s): unexpected error: %s", user_id, e, exc_info=True)
        return 500, f"{user_id} : {traceback.format_exc()}\n"
