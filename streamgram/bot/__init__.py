# (c) adarsh-goel
import logging
import os
from pyrogram import Client
from ..vars import Var

logger = logging.getLogger("streamgram.bot")

# Pyrogram's *.session files default to the current working directory. Keeping them in their
# own subdirectory (not mixed in with the application code) matters for Docker specifically:
# a volume mounted to persist sessions across container recreation only needs to cover this
# directory, not the whole app -- mounting the whole app directory (as the plain `docker run`
# docs used to suggest) freezes it at whatever was there when the volume was first created,
# so a later image rebuild's code changes silently never take effect. Confirmed by actually
# hitting this running the bot via docker compose.
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

logger.debug("Constructing the main StreamBot client (workers=%s, sleep_threshold=%s)", Var.WORKERS, Var.SLEEP_THRESHOLD)
StreamBot = Client(
    name='Web Streamer',
    api_id=Var.API_ID,
    api_hash=Var.API_HASH,
    bot_token=Var.BOT_TOKEN,
    sleep_threshold=Var.SLEEP_THRESHOLD,
    workers=Var.WORKERS,
    workdir=SESSIONS_DIR,
)

multi_clients = {}
work_loads = {}
