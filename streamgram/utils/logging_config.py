"""Central logging setup.

Two files, both size-limited and rotated, so logs cannot grow without bound:
  <dir>/info.log   everything below WARNING that passes the configured level (INFO, and DEBUG if enabled)
  <dir>/error.log  WARNING, ERROR and CRITICAL
stderr (what `pm2 logs` / `docker logs` / a plain terminal show) mirrors the same configured
level -- if LOG_LEVEL=DEBUG, debug lines show up live on the console, not just in info.log.
Previously the console was hard-capped at ERROR regardless of LOG_LEVEL, so setting LOG_LEVEL=DEBUG
to watch something live produced no visible output at all; the detail was only ever in the files.
The level comes from LOG_LEVEL (default INFO); noisy libraries are held at LOG_LIBS_LEVEL (default WARNING).
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LIBRARIES = ("pyrogram", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "starlette",
             "asyncio", "motor", "pymongo", "aiohttp", "apscheduler")


def to_level(value, default=logging.INFO) -> int:
    """'debug' / 'INFO' / 20 -> a logging level number; anything unknown -> default."""
    if isinstance(value, int):
        return value
    level = logging.getLevelName(str(value).strip().upper())
    result = level if isinstance(level, int) else default
    logging.getLogger("streamgram.utils.logging_config").debug("to_level(%r) -> %s", value, result)
    return result


def setup_logging(level="INFO", log_dir="logs", max_bytes=5 * 1024 * 1024, backups=3, libs_level="WARNING"):
    level_no = to_level(level)
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter(FORMAT, DATE_FORMAT)

    info_handler = RotatingFileHandler(os.path.join(log_dir, "info.log"), maxBytes=max_bytes,
                                       backupCount=backups, encoding="utf-8")
    info_handler.setLevel(level_no)
    info_handler.addFilter(lambda record: record.levelno < logging.WARNING)

    error_handler = RotatingFileHandler(os.path.join(log_dir, "error.log"), maxBytes=max_bytes,
                                        backupCount=backups, encoding="utf-8")
    error_handler.setLevel(max(level_no, logging.WARNING))

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level_no)

    for handler in (info_handler, error_handler, console):
        handler.setFormatter(formatter)

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.setLevel(level_no)
    for handler in (info_handler, error_handler, console):
        root.addHandler(handler)

    lib_no = max(to_level(libs_level, logging.WARNING), level_no if level_no > logging.WARNING else 0)
    for name in LIBRARIES:
        logging.getLogger(name).setLevel(lib_no)
    # Handlers are attached above, so this is the first record that actually lands in the files.
    root.info(
        "Logging configured: level=%s dir=%s max_bytes=%s backups=%s libs_level=%s",
        logging.getLevelName(level_no), log_dir, max_bytes, backups, logging.getLevelName(lib_no),
    )
    return root
