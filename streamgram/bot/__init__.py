# (c) adarsh-goel
import logging
from pyrogram import Client
from ..vars import Var
from os import getcwd

logger = logging.getLogger("streamgram.bot")

logger.debug("Constructing the main StreamBot client (workers=%s, sleep_threshold=%s)", Var.WORKERS, Var.SLEEP_THRESHOLD)
StreamBot = Client(
    name='Web Streamer',
    api_id=Var.API_ID,
    api_hash=Var.API_HASH,
    bot_token=Var.BOT_TOKEN,
    sleep_threshold=Var.SLEEP_THRESHOLD,
    workers=Var.WORKERS
)

multi_clients = {}
work_loads = {}
