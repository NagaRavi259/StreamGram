import asyncio
import logging
logger = logging.getLogger("streamgram.utils.keepalive")
import aiohttp
from streamgram.vars import Var


async def ping_server():
    sleep_time = Var.PING_INTERVAL
    logger.info("Keep-alive ping task started (every %ss, target %s)", sleep_time, Var.URL)
    while True:
        await asyncio.sleep(sleep_time)
        logger.debug("Pinging %s", Var.URL)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(Var.URL) as resp:
                    logger.info("Pinged server with response: {}".format(resp.status))
        except TimeoutError:
            logger.warning("Couldn't connect to the site URL..!")
        except Exception:
            logger.exception("Keep-alive ping failed")
