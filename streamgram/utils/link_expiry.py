"""Automatic link expiry (self-contained module).

Every link the bot creates points at a message in the bin channel. When a link is created for a
person, `register()` looks up how long links should live for that person and, if the answer is
finite, stores an expiry time for the message. The web server asks `is_expired()` on each request.

Settings (all kept in MongoDB, managed from /admin):
  * default lifetime for everyone   -> settings document `link_ttl`          (unset = unlimited)
  * personal lifetime for one user  -> `link_ttl` field on the user document (overrides the default;
                                       {"seconds": None} means unlimited for that user)

Links created before this module existed (or while the lifetime was unlimited) have no record and
never expire. Records are never deleted: removing an expired record would make the link live again.
"""
import logging
import re
import time

from streamgram.utils.access import access_db, fmt_duration

logger = logging.getLogger("streamgram.utils.link_expiry")

# (button label, seconds)
PRESETS = [("1 hour", 3600), ("6 hours", 6 * 3600), ("1 day", 86400), ("7 days", 7 * 86400), ("30 days", 30 * 86400)]
MAX_SECONDS = 3650 * 86400
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_TOKEN = re.compile(r"(\d+)([smhdw])")


def parse_duration(text: str) -> int:
    """'90m', '12h', '3d', '2w', '1d12h' -> seconds. Raises ValueError with a hint on bad input."""
    cleaned = re.sub(r"\s+", "", str(text).lower())
    pos, total = 0, 0
    for m in _TOKEN.finditer(cleaned):
        if m.start() != pos:
            break
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if not cleaned or pos != len(cleaned) or total <= 0 or total > MAX_SECONDS:
        logger.debug("parse_duration(%r): could not parse", text)
        raise ValueError("Use a number with a unit, e.g. 30m, 12h, 3d, 2w (or 1d12h)")
    logger.debug("parse_duration(%r) -> %ss", text, total)
    return total


def format_ttl(seconds) -> str:
    result = "unlimited" if seconds is None else fmt_duration(seconds)
    logger.debug("format_ttl(%s) -> %r", seconds, result)
    return result


class LinkExpiry:
    CACHE_SECONDS = 60

    def __init__(self, db):
        self._db = db  # collections are read from it on use, so they can be swapped (tests, reconnects)
        self._cache = {}  # message id -> (checked at, expires_at or None)

    users = property(lambda self: self._db.users)
    settings = property(lambda self: self._db.settings)
    links = property(lambda self: self._db.links)

    # ---- settings
    async def get_default(self):
        doc = await self.settings.find_one({"_id": "link_ttl"})
        seconds = doc.get("seconds") if doc else None
        logger.debug("get_default() -> %s", seconds)
        return seconds

    async def set_default(self, seconds):
        logger.info("set_default(%s)", seconds)
        await self.settings.update_one({"_id": "link_ttl"}, {"$set": {"seconds": seconds}}, upsert=True)

    async def get_personal(self, uid):
        """(has_personal_setting, seconds_or_None)."""
        rec = await self.users.find_one({"id": int(uid)}, {"link_ttl": 1})
        if rec and rec.get("link_ttl") is not None:
            return True, rec["link_ttl"].get("seconds")
        return False, None

    async def set_personal(self, uid, seconds):
        logger.info("set_personal(%s, %s)", uid, seconds)
        await self.users.update_one(
            {"id": int(uid)}, {"$set": {"link_ttl": {"seconds": seconds}}, "$setOnInsert": {"created_at": int(time.time())}},
            upsert=True,
        )

    async def clear_personal(self, uid):
        logger.info("clear_personal(%s): back to the default lifetime", uid)
        await self.users.update_one({"id": int(uid)}, {"$unset": {"link_ttl": ""}})

    async def effective(self, uid):
        """(seconds_or_None, source) where source is 'personal' or 'default'."""
        personal, seconds = await self.get_personal(uid)
        if personal:
            logger.debug("effective(%s) -> %s (personal)", uid, seconds)
            return seconds, "personal"
        seconds = await self.get_default()
        logger.debug("effective(%s) -> %s (default)", uid, seconds)
        return seconds, "default"

    # ---- links
    async def register(self, msg_id, uid):
        """Called when a link is created. Returns the expiry time (epoch seconds) or None if unlimited."""
        seconds, _ = await self.effective(uid)
        if seconds is None:
            return None
        now = time.time()
        expires_at = now + seconds
        await self.links.update_one(
            {"msg_id": int(msg_id)},
            {"$set": {"msg_id": int(msg_id), "uid": int(uid), "created_at": int(now), "expires_at": expires_at}},
            upsert=True,
        )
        self._cache[int(msg_id)] = (time.time(), expires_at)
        logger.debug(f"Link {msg_id} for {uid} expires in {seconds}s")
        return expires_at

    async def expires_at(self, msg_id):
        msg_id = int(msg_id)
        hit = self._cache.get(msg_id)
        if hit and time.time() - hit[0] < self.CACHE_SECONDS:
            logger.debug("expires_at(%s): cache hit -> %s", msg_id, hit[1])
            return hit[1]
        try:
            rec = await self.links.find_one({"msg_id": msg_id}, {"expires_at": 1})
        except Exception as e:
            # A database hiccup must not break downloads: keep the last known answer (or treat the
            # link as unlimited) and look again in about 10 seconds.
            logger.warning(f"Could not read the expiry of link {msg_id}: {e}")
            known = hit[1] if hit else None
            self._cache[msg_id] = (time.time() - self.CACHE_SECONDS + 10, known)
            return known
        expires_at = rec.get("expires_at") if rec else None
        if len(self._cache) > 10000:
            logger.debug("expires_at: cache grew past 10000 entries, clearing it")
            self._cache.clear()
        self._cache[msg_id] = (time.time(), expires_at)
        logger.debug("expires_at(%s) -> %s", msg_id, expires_at)
        return expires_at

    async def is_expired(self, msg_id) -> bool:
        expires_at = await self.expires_at(msg_id)
        expired = expires_at is not None and time.time() >= expires_at
        if expired:
            logger.info("is_expired(%s): link has expired", msg_id)
        return expired


link_expiry = LinkExpiry(access_db)
