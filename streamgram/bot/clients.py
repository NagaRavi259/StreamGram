# (c) adarsh-goel

import asyncio
import logging
logger = logging.getLogger("streamgram.bot.clients")
from ..vars import Var
from pyrogram import Client
from streamgram.utils.config_parser import TokenParser
from . import multi_clients, work_loads, StreamBot


async def initialize_clients():
    multi_clients[0] = StreamBot
    work_loads[0] = 0
    all_tokens = TokenParser().parse_from_env()
    if not all_tokens:
        logger.info("No additional clients found, using default client")
        return

    async def start_client(client_id, token):
        try:
            logger.info(f"Starting - Client {client_id}")
            if client_id == len(all_tokens):
                await asyncio.sleep(2)
                logger.info("This will take some time, please wait...")
            client = await Client(
                name=f"memory_{client_id}",
                api_id=Var.API_ID,
                api_hash=Var.API_HASH,
                bot_token=token,
                sleep_threshold=Var.SLEEP_THRESHOLD,
                no_updates=True,
            ).start()
            work_loads[client_id] = 0
            logger.debug(f"Client {client_id} started successfully")
            return client_id, client
        except Exception:
            logger.error(f"Failed starting Client - {client_id} Error:", exc_info=True)

    clients = await asyncio.gather(*[start_client(i, token) for i, token in all_tokens.items()])
    started = [c for c in clients if c is not None]
    failed = len(clients) - len(started)
    if failed:
        logger.warning(f"{failed} of {len(clients)} MULTI_TOKEN_* client(s) failed to start and were skipped")
    multi_clients.update(dict(started))
    if len(multi_clients) != 1:
        Var.MULTI_CLIENT = True
        logger.info("Multi-Client Mode Enabled")
    else:
        logger.info("No additional clients were initialized, using default client")
