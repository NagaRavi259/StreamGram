# (c) adarsh-goel
import logging

logger = logging.getLogger("streamgram.utils.human_readable")


def humanbytes(size):
    # https://stackoverflow.com/a/49361727/4723940
    # 2**10 = 1024
    if not size:
        logger.debug("humanbytes(%r) -> '' (falsy size)", size)
        return ""
    power = 2**10
    n = 0
    units = ('', 'Ki', 'Mi', 'Gi', 'Ti')
    while size >= power and n < len(units) - 1:
        size /= power
        n += 1
    result = f"{round(size, 2)} {units[n]}B"
    logger.debug("humanbytes -> %s", result)
    return result
