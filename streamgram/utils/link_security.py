"""Per-link secret tokens (C2).

The original scheme put a prefix of Telegram's `file_unique_id` in the URL (see
`file_properties.get_hash`/`hash_ok`) — short, and derived from an id that is not actually a
secret (anyone who can see the file elsewhere can read it off, and ids are somewhat guessable).

New links instead get a random, unguessable token stored against the bin-channel message id.
`check()` falls back to the old `file_unique_id`-prefix scheme only for links that predate this
module (no stored token for that message), so links already handed out keep working.
"""
import logging
import secrets
import time

from streamgram.utils.access import access_db
from streamgram.utils.file_properties import hash_ok

logger = logging.getLogger("streamgram.utils.link_security")

TOKEN_BYTES = 9  # secrets.token_urlsafe(9) -> 12 URL-safe characters, ~72 bits of entropy
CACHE_SECONDS = 60


class LinkTokens:
    def __init__(self, db):
        self._db = db  # read from it on use, so it can be swapped (tests, reconnects)
        self._cache = {}  # msg_id -> (checked_at, token_or_None)

    tokens = property(lambda self: self._db.link_tokens)

    async def issue(self, msg_id) -> str:
        """Called once, when a link is created. Returns the token to put in the URL."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        msg_id = int(msg_id)
        await self.tokens.update_one(
            {"msg_id": msg_id},
            {"$set": {"msg_id": msg_id, "token": token, "created_at": int(time.time())}},
            upsert=True,
        )
        self._cache[msg_id] = (time.time(), token)
        logger.info("issue(%s): new link token issued", msg_id)
        return token

    async def _get(self, msg_id):
        msg_id = int(msg_id)
        hit = self._cache.get(msg_id)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            logger.debug("_get(%s): cache hit", msg_id)
            return hit[1]
        try:
            rec = await self.tokens.find_one({"msg_id": msg_id}, {"token": 1})
        except Exception as e:
            # A database hiccup must not break downloads: keep the last known answer.
            logger.warning("Could not read the token for link %s: %s", msg_id, e)
            return hit[1] if hit else None
        token = rec.get("token") if rec else None
        self._cache[msg_id] = (time.time(), token)
        logger.debug("_get(%s): %s", msg_id, "has a token" if token else "no token (legacy link)")
        return token

    async def check(self, msg_id, unique_id, secure_hash) -> bool:
        """True if secure_hash is valid for this message: the issued token (exact match) for
        links created after tokenisation, or the old file_unique_id-prefix hash for links that
        predate it (no token on record for that message)."""
        token = await self._get(msg_id)
        if token is not None:
            ok = secure_hash == token
            if not ok:
                logger.debug("check(%s): hash does not match the issued token", msg_id)
            return ok
        ok = hash_ok(unique_id, secure_hash)
        logger.debug("check(%s): legacy hash_ok -> %s", msg_id, ok)
        return ok


link_tokens = LinkTokens(access_db)
