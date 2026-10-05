# (c) adarsh-goel
import os
import sys
import glob
import asyncio
import logging
import importlib
from pathlib import Path
from pyrogram import idle
from .bot import StreamBot
from .vars import Var
from .utils.logging_config import setup_logging
import uvicorn
from .server import app  # Import FastAPI app instance
from .utils.keepalive import ping_server
from streamgram.bot.clients import initialize_clients

setup_logging(Var.LOG_LEVEL, Var.LOG_DIR, Var.LOG_MAX_MB * 1024 * 1024, Var.LOG_BACKUPS, Var.LOG_LIBS_LEVEL)
logger = logging.getLogger("streamgram")
logger.info("Logging ready (level %s, files in %s/)", Var.LOG_LEVEL.upper(), Var.LOG_DIR)

ppath = "streamgram/bot/plugins/*.py"
files = glob.glob(ppath)
logger.debug("Found %d plugin file(s) under %s", len(files), ppath)
StreamBot.start()
logger.info("StreamBot logged in to Telegram")
loop = asyncio.get_event_loop()

async def start_services():
    print('\n')
    print('------------------- Initalizing Telegram Bot -------------------')
    bot_info = await StreamBot.get_me()
    StreamBot.username = bot_info.username
    logger.info("Bot identity resolved: @%s (id %s)", StreamBot.username, bot_info.id)
    print("------------------------------ DONE ------------------------------")
    print()
    print(
        "---------------------- Initializing Clients ----------------------"
    )
    await initialize_clients()
    logger.info("Multi-client initialization finished")
    print("------------------------------ DONE ------------------------------")
    print('\n')
    print('--------------------------- Importing ---------------------------')
    for name in files:
        with open(name) as a:
            patt = Path(a.name)
            plugin_name = patt.stem.replace(".py", "")
            plugins_dir = Path(f"streamgram/bot/plugins/{plugin_name}.py")
            import_path = ".plugins.{}".format(plugin_name)
            spec = importlib.util.spec_from_file_location(import_path, plugins_dir)
            load = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(load)
            except Exception:
                logger.exception("Failed to load plugin %s", plugin_name)
                raise
            sys.modules["streamgram.bot.plugins." + plugin_name] = load
            logger.info("Loaded plugin: %s", plugin_name)
            print("Imported => " + plugin_name)
    if Var.ON_HEROKU:
        print("------------------ Starting Keep Alive Service ------------------")
        print()
        logger.info("Starting keep-alive ping task (Heroku/Railway)")
        asyncio.create_task(ping_server())
    print('-------------------- Initalizing Web Server -------------------------')
    # Use uvicorn to run FastAPI app
    config = uvicorn.Config(app, host=Var.BIND_ADRESS, port=Var.PORT, log_level="error")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    logger.info("Web server starting on %s:%s", Var.BIND_ADRESS, Var.PORT)
    print('----------------------------- DONE ---------------------------------------------------------------------')
    print('\n')
    print('----------------------- Service Started -----------------------------------------------------------------')
    print('                        bot =>> {}'.format((await StreamBot.get_me()).first_name))
    print('                        server ip =>> {}:{}'.format(Var.BIND_ADRESS, Var.PORT))
    print('                        Owner =>> {}'.format((Var.OWNER_USERNAME)))
    if Var.ON_HEROKU:
        print('                        app runnng on =>> {}'.format(Var.FQDN))
    print('---------------------------------------------------------------------------------------------------------')
    logger.info("Service started: bot=@%s, web=%s:%s", StreamBot.username, Var.BIND_ADRESS, Var.PORT)
    await idle()
    logger.warning("idle() returned: the bot is shutting down")

if __name__ == '__main__':
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        logger.info('Service stopped')
    except Exception:
        logger.exception('Service crashed')
        raise
