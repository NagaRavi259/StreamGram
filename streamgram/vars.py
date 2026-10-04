# (c) adarsh-goel
import os
from os import getenv, environ
from dotenv import load_dotenv
from urllib.parse import quote_plus

env_path = "config.env"

load_dotenv(dotenv_path=env_path)

def _env_bool(name, default=False):
    """True for 1/true/yes/on (any case); 'false', '0', '' and unset are False."""
    value = getenv(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _env_text(name):
    """The value, or None when unset, blank or the literal text 'none'."""
    value = (getenv(name) or "").strip()
    return None if value.lower() in ("", "none") else value


class Var(object):
    MULTI_CLIENT = False
    API_ID = int(getenv('API_ID'))
    API_HASH = str(getenv('API_HASH'))
    BOT_TOKEN = str(getenv('BOT_TOKEN'))
    name = str(getenv('SESSION_NAME', 'streamgram'))
    SLEEP_THRESHOLD = int(getenv('SLEEP_THRESHOLD', '60'))
    BIN_CHANNEL = int(getenv('BIN_CHANNEL'))
    PORT = int(getenv('PORT', 8080))
    BIND_ADRESS = str(getenv('WEB_SERVER_BIND_ADDRESS', '0.0.0.0'))
    PING_INTERVAL = int(environ.get("PING_INTERVAL", "1200"))  # 20 minutes
    OWNER_ID = set(int(x) for x in os.environ.get("OWNER_ID", "").replace(',', ' ').split())
    NO_PORT = _env_bool('NO_PORT')
    APP_NAME = None
    OWNER_USERNAME = str(getenv('OWNER_USERNAME'))
    if 'DYNO' in environ:
        ON_HEROKU = True
        APP_NAME = str(getenv('APP_NAME'))

    else:
        ON_HEROKU = False
    FQDN = str(getenv('FQDN', BIND_ADRESS)) if not ON_HEROKU or getenv('FQDN') else APP_NAME+'.railway.app'
    HAS_SSL = _env_bool('HAS_SSL')
    if HAS_SSL:
        URL = "https://{}/".format(FQDN)
    else:
        URL = "http://{}/".format(FQDN)
    WORKERS = int(getenv('WORKERS', 3))
    MY_PASS = _env_text('MY_PASS')

    ## database connection detials
    # DATABASE_URL = str(getenv('DATABASE_URL'))

    MONGO_SCHEMA = getenv("MONGO_SCHEMA", "")
    MONGO_USERNAME = quote_plus(getenv("MONGO_USERNAME", ""))
    MONGO_PASSWORD = quote_plus(getenv("MONGO_PASSWORD", ""))  # this will encode the colon (:) and any other special characters
    MONGO_HOST = getenv("MONGO_HOST", "127.0.0.1")
    MONGO_PORT = getenv("MONGO_PORT", '27017')

    UPDATES_CHANNEL = _env_text('UPDATES_CHANNEL')  # None = no forced subscription
    BANNED_CHANNELS = list(set(int(x) for x in str(getenv("BANNED_CHANNELS", "-1001362659779")).split()))
    # ids separated by spaces and/or commas; blank or unset means nobody
    TRUSTED_USERS = set(int(u) for u in (getenv('TRUSTED_USERS') or '').replace(',', ' ').split())
    USER_GROUP_ID = int(getenv('USER_GROUP_ID', 1))

    # logging (see streamgram/utils/logging_config.py)
    LOG_LEVEL = str(getenv('LOG_LEVEL', 'INFO'))            # DEBUG, INFO, WARNING, ERROR
    LOG_LIBS_LEVEL = str(getenv('LOG_LIBS_LEVEL', 'WARNING'))  # pyrogram, uvicorn, motor ...
    LOG_DIR = str(getenv('LOG_DIR', 'logs'))
    LOG_MAX_MB = int(getenv('LOG_MAX_MB', 5))               # size of each log file before it rotates
    LOG_BACKUPS = int(getenv('LOG_BACKUPS', 3))             # rotated copies kept per file
