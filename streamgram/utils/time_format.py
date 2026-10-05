# (c) adarsh-goel
# Bot Uptime
import logging

logger = logging.getLogger("streamgram.utils.time_format")


def get_readable_time(seconds: int) -> str:
    if seconds < 0:
        logger.warning("get_readable_time called with a negative value: %s", seconds)
    count = 0
    readable_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", " days"]
    while count < 4:
        count += 1
        if count < 3:
            remainder, result = divmod(seconds, 60)
        else:
            remainder, result = divmod(seconds, 24)
        if seconds == 0 and remainder == 0:
            break
        time_list.append(int(result))
        seconds = int(remainder)
    for x in range(len(time_list)):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]
    if len(time_list) == 4:
        readable_time += time_list.pop() + ", "
    time_list.reverse()
    readable_time += ": ".join(time_list)
    logger.debug("get_readable_time -> %r", readable_time)
    return readable_time
