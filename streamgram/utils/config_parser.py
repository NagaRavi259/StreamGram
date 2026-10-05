import logging
from os import environ
from typing import Dict, Optional

logger = logging.getLogger("streamgram.utils.config_parser")


class TokenParser:
    def __init__(self, config_file: Optional[str] = None):
        self.tokens = {}
        self.config_file = config_file
        logger.debug("TokenParser created (config_file=%s)", config_file)

    def parse_from_env(self) -> Dict[int, str]:
        self.tokens = dict(
            (c + 1, t)
            for c, (_, t) in enumerate(
                filter(
                    lambda n: n[0].startswith("MULTI_TOKEN"), sorted(environ.items())
                )
            )
        )
        if self.tokens:
            logger.info("Found %d MULTI_TOKEN_* client(s) in the environment", len(self.tokens))
        else:
            logger.debug("No MULTI_TOKEN_* variables found")
        return self.tokens
