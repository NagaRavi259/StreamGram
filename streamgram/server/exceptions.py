import logging
logger = logging.getLogger("streamgram.server.exceptions")


class InvalidHash(Exception):
    message = "Invalid hash"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logger.debug("InvalidHash raised")


class FIleNotFound(Exception):
    message = "File not found"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logger.debug("FIleNotFound raised")


class LinkExpired(Exception):
    message = "This link has expired"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logger.debug("LinkExpired raised")
