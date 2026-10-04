import asyncio
import logging
logger = logging.getLogger("streamgram.utils.keepalive")
import aiohttp
import traceback
from streamgram.vars import Var


async def ping_server():
    sleep_time = Var.PING_INTERVAL
    while True:
        await asyncio.sleep(sleep_time)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(Var.URL) as resp:
                    logger.info("Pinged server with response: {}".format(resp.status))
        except TimeoutError:
            logger.warning("Couldn't connect to the site URL..!")
        except Exception:
            traceback.print_exc()
