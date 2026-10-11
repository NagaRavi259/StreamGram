# (c) adarsh-goel
import logging
import os
from os import getenv, environ
from dotenv import load_dotenv
from urllib.parse import quote_plus

# This module runs before streamgram.utils.logging_config.setup_logging(), so anything logged here
# below WARNING is invisible until a handler is attached later (Python's "handler of last
# resort" only prints WARNING+). That's fine: this is exactly the startup-sanity-check case.
logger = logging.getLogger("streamgram.vars")

env_path = "config.env"

logger.debug("Loading config from %s (falls back to real environment variables if absent)", env_path)
load_dotenv(dotenv_path=env_path)

def _env_bool(name, default=False):
    """True for 1/true/yes/on (any case); 'false', '0', '' and unset are False."""
    value = getenv(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _env_text(name):
    """The value, or None when unset, blank or the literal text 'none'."""
    value = (getenv(name) or "").strip()
    return None if value.lower() in ("", "none") else value


def _env_int(name, default):
    """default when unset or blank (e.g. `VAR=` in config.env), otherwise int(value).

    getenv(name, default) alone only substitutes the default when the variable is
    completely absent -- an env var present but set to an empty string (exactly what
    config.env.example's own convention produces for an unused optional variable) still
    comes back as '', and int('') raises ValueError. Confirmed live via docker compose:
    config.env's documented `USER_GROUP_ID=` crashed startup before this helper existed.
    """
    value = getenv(name)
    return default if value is None or not value.strip() else int(value)


class Var(object):
    MULTI_CLIENT = False
    API_ID = int(getenv('API_ID'))
    API_HASH = str(getenv('API_HASH'))
    BOT_TOKEN = str(getenv('BOT_TOKEN'))
    name = str(getenv('SESSION_NAME', 'streamgram'))
    SLEEP_THRESHOLD = _env_int('SLEEP_THRESHOLD', 60)
    BIN_CHANNEL = int(getenv('BIN_CHANNEL'))
    PORT = _env_int('PORT', 8080)
    BIND_ADRESS = str(getenv('WEB_SERVER_BIND_ADDRESS', '0.0.0.0'))
    PING_INTERVAL = _env_int('PING_INTERVAL', 1200)  # 20 minutes
    OWNER_ID = set(int(x) for x in os.environ.get("OWNER_ID", "").replace(',', ' ').split())
    if not OWNER_ID:
        logger.warning("OWNER_ID is empty: nobody is exempt from access checks and admin commands are unreachable.")
    NO_PORT = _env_bool('NO_PORT')
    APP_NAME = None
    OWNER_USERNAME = str(getenv('OWNER_USERNAME'))
    if 'DYNO' in environ:
        ON_HEROKU = True
        APP_NAME = str(getenv('APP_NAME'))
        logger.info("Running on Heroku/Railway (DYNO set): keep-alive pinging is enabled.")
    else:
        ON_HEROKU = False
    FQDN = str(getenv('FQDN', BIND_ADRESS)) if not ON_HEROKU or getenv('FQDN') else APP_NAME+'.railway.app'
    HAS_SSL = _env_bool('HAS_SSL')
    if HAS_SSL:
        URL = "https://{}/".format(FQDN)
    else:
        URL = "http://{}/".format(FQDN)
    logger.debug("Public URL resolved to %s", URL)
    WORKERS = _env_int('WORKERS', 3)
    MY_PASS = _env_text('MY_PASS')
    logger.info("Password login (/login) is %s", "ENABLED" if MY_PASS else "disabled (MY_PASS unset)")

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
    USER_GROUP_ID = _env_int('USER_GROUP_ID', 1)
    if USER_GROUP_ID >= 0:
        logger.warning(
            "USER_GROUP_ID (%s) is not a negative chat id: no access group is configured, "
            "so only OWNER_ID/TRUSTED_USERS and explicitly approved users get in.", USER_GROUP_ID
        )
    else:
        logger.info("Access group configured: USER_GROUP_ID=%s", USER_GROUP_ID)

    # Audio-fix transcoding (see streamgram/utils/audio_fix.py): browsers can't decode AC3/E-AC3/DTS/
    # TrueHD audio natively, so a file with one of those tracks plays video with no sound. When
    # enabled, such files are served through a narrow ffmpeg pass that copies video untouched and
    # re-encodes only the audio to AAC. Needs the ffmpeg/ffprobe binaries on PATH; silently stays
    # off (falls back to today's plain passthrough) if they're missing.
    ENABLE_AUDIO_FIX = _env_bool('ENABLE_AUDIO_FIX', True)
    MAX_CONCURRENT_AUDIO_FIX = _env_int('MAX_CONCURRENT_AUDIO_FIX', 2)

    # logging (see streamgram/utils/logging_config.py)
    LOG_LEVEL = str(getenv('LOG_LEVEL', 'INFO'))            # DEBUG, INFO, WARNING, ERROR
    LOG_LIBS_LEVEL = str(getenv('LOG_LIBS_LEVEL', 'WARNING'))  # pyrogram, uvicorn, motor ...
    LOG_DIR = str(getenv('LOG_DIR', 'logs'))
    LOG_MAX_MB = _env_int('LOG_MAX_MB', 5)               # size of each log file before it rotates
    LOG_BACKUPS = _env_int('LOG_BACKUPS', 3)             # rotated copies kept per file
    logger.debug(
        "Config loaded: name=%s PORT=%s BIND=%s ON_HEROKU=%s LOG_LEVEL=%s",
        name, PORT, BIND_ADRESS, ON_HEROKU, LOG_LEVEL,
    )
