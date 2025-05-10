# (c) adarsh-goel
import os
import sys
import glob
import asyncio
import logging
import datetime
from logging.handlers import RotatingFileHandler
import importlib
from pathlib import Path
from pyrogram import idle
from .bot import StreamBot
from .vars import Var
import uvicorn
from .server import app  # Import FastAPI app instance
from .utils.keepalive import ping_server
from streamgram.bot.clients import initialize_clients

# Generate a timestamp
timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

# Set up the logging configuration with a timestamped filename
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RotatingFileHandler(
            f'logs/log_{timestamp}.log',  # Dynamic filename with timestamp
            # maxBytes=1000000,  # Max size in bytes before rotating
            backupCount=5,  # Number of backup files to keep
            encoding='utf-8'  # Ensure correct file encoding
        )
    ]
)

log_level = logging.ERROR
# Setting logging levels for specific modules
logging.getLogger("fastapi").setLevel(log_level)
logging.getLogger("starlette").setLevel(log_level)
logging.getLogger("uvicorn").setLevel(log_level)
logging.getLogger("uvicorn.access").setLevel(log_level)
logging.getLogger("uvicorn.error").setLevel(log_level)

# Setting DEBUG level for Pyrogram, which is a Telegram client library
logging.getLogger("pyrogram").setLevel(log_level)

# Example of logging usage
logging.info("Logging system with RotatingFileHandler is set up and ready.")

ppath = "streamgram/bot/plugins/*.py"
files = glob.glob(ppath)
StreamBot.start()
loop = asyncio.get_event_loop()

async def start_services():
    print('\n')
    print('------------------- Initalizing Telegram Bot -------------------')
    bot_info = await StreamBot.get_me()
    StreamBot.username = bot_info.username
    print("------------------------------ DONE ------------------------------")
    print()
    print(
        "---------------------- Initializing Clients ----------------------"
    )
    await initialize_clients()
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
            spec.loader.exec_module(load)
            sys.modules["streamgram.bot.plugins." + plugin_name] = load
            print("Imported => " + plugin_name)
    if Var.ON_HEROKU:
        print("------------------ Starting Keep Alive Service ------------------")
        print()
        asyncio.create_task(ping_server())
    print('-------------------- Initalizing Web Server -------------------------')
    # Use uvicorn to run FastAPI app
    config = uvicorn.Config(app, host=Var.BIND_ADRESS, port=Var.PORT, log_level="debug")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    print('----------------------------- DONE ---------------------------------------------------------------------')
    print('\n')
    print('----------------------- Service Started -----------------------------------------------------------------')
    print('                        bot =>> {}'.format((await StreamBot.get_me()).first_name))
    print('                        server ip =>> {}:{}'.format(Var.BIND_ADRESS, Var.PORT))
    print('                        Owner =>> {}'.format((Var.OWNER_USERNAME)))
    if Var.ON_HEROKU:
        print('                        app runnng on =>> {}'.format(Var.FQDN))
    print('---------------------------------------------------------------------------------------------------------')
    await idle()

if __name__ == '__main__':
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        logging.info('----------------------- Service Stopped -----------------------')
