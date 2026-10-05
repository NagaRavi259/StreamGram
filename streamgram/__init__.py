# (c) adarsh-goel

import logging
import time

logger = logging.getLogger("streamgram")

StartTime = time.time()
__version__ = 1.1
logger.debug("streamgram package imported (version %s, StartTime %s)", __version__, StartTime)
