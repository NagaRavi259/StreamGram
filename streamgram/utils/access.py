"""Access control: approved users, bans, history, access groups, invites and usage limits.

Mongo collections (database = Var.name):
  access_users    one doc per person who requested / was granted / was banned
  access_history  append-only log of every access decision
  access_usage    one doc per generated link (used for rate limits)
  access_chats    groups the bot has seen; `enabled` ones grant access to their members
  access_invites  one-off invite tokens
  access_settings default limit
  join_requests   people who asked to join an access group (recorded by access_admin plugin)
"""
import time
import secrets
import logging
import motor.motor_asyncio
from pyrogram import enums
from pyrogram.errors import UserNotParticipant, PeerIdInvalid
from streamgram.vars import Var
from streamgram.utils.database import get_mongo_uri

# Rolling windows, in seconds. "life" = all time, "unlimited" = no limit.
PERIODS = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}
PERIOD_LABELS = {
    "hour": "hour", "day": "day", "week": "week", "month": "month",
    "life": "lifetime", "unlimited": "unlimited",
}
INVITE_TTL = 7 * 86400
ACCESS_STATUSES = ('approved', 'group')  # statuses listed as 'users with access'
MEMBER_STATUSES = (
    enums.ChatMemberStatus.OWNER,
    enums.ChatMemberStatus.ADMINISTRATOR,
    enums.ChatMemberStatus.MEMBER,
)


def is_exempt(user_id: int) -> bool:
    """Owners and TRUSTED_USERS bypass access checks and limits."""
    return user_id in Var.OWNER_ID or user_id in Var.TRUSTED_USERS


def profile_of(user) -> dict:
    return {
        "first_name": user.first_name or "",
        "username": user.username or "",
    }


def describe_quota(period, limit) -> str:
    if not period or period == "unlimited" or limit is None:
        return "unlimited"
    if period == "life":
        return f"{limit} total (lifetime)"
    return f"{limit} per {period}"


def fmt_duration(seconds: int) -> str:
    seconds = max(int(seconds), 1)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else ""]
    out = " ".join(p for p in parts if p)
    return out or f"{s}s"


class AccessDB:
    def __init__(self):
        client = motor.motor_asyncio.AsyncIOMotorClient(get_mongo_uri())
        db = client[Var.name]
        self.users = db.access_users
        self.history = db.access_history
        self.usage = db.access_usage
        self.chats = db.access_chats
        self.invites = db.access_invites
        self.settings = db.access_settings
        self.join_requests = db.join_requests
        self._group_cache = {"ts": 0, "ids": []}
        self._status_cache = {}
        self._touched = {}
        self._warned = {}

    async def init(self):
        try:
            await self.users.create_index("id", unique=True)
            await self.usage.create_index([("uid", 1), ("ts", -1)])
            await self.history.create_index([("ts", -1)])
            await self.invites.create_index("token", unique=True)
        except Exception as e:
            logging.error(f"AccessDB index creation failed: {e}")

    # ---------------- users ----------------
    async def get_user(self, uid):
        return await self.users.find_one({"id": int(uid)})

    async def set_status(self, uid, status, by=0, profile=None, source=None, note=""):
        now = int(time.time())
        fields = {"status": status, "updated_at": now}
        if profile:
            fields.update(profile)
        if status == "approved":
            fields["granted_at"] = now
            fields["granted_by"] = by
            if source:
                fields["source"] = source
        elif status == "banned":
            fields["banned_at"] = now
        elif status == "pending":
            fields["requested_at"] = now
        await self.users.update_one(
            {"id": int(uid)},
            {"$set": fields, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        await self.log(uid, status, by, note)

    async def list_users(self, statuses, skip=0, limit=8, sort_field="last_used"):
        """statuses: list of status values. Newest `sort_field` first; never-used entries last."""
        cur = (
            self.users.find({"status": {"$in": list(statuses)}})
            .sort([(sort_field, -1), ("granted_at", -1)])
            .skip(skip).limit(limit)
        )
        return await cur.to_list(limit)

    async def count_users(self, statuses):
        return await self.users.count_documents({"status": {"$in": list(statuses)}})

    async def touch(self, user):
        """Record that an allowed user just used the bot (throttled to one write per minute).

        People who got in only through a group / join request have no record yet, so one is
        created with status 'group'. Access for them is still decided live by has_access().
        """
        now = time.time()
        if now - self._touched.get(user.id, 0) < 60:
            return
        self._touched[user.id] = now
        rec = await self.users.find_one({"id": user.id}, {"status": 1})
        fields = {"last_used": int(now), **profile_of(user)}
        if rec is None:
            await self.users.insert_one(
                {"id": user.id, "status": "group", "source": "group", "created_at": int(now),
                 "updated_at": int(now), **fields}
            )
        else:
            if rec.get("status") not in ("approved", "group", "banned"):
                fields.update(status="group", source="group", updated_at=int(now))
            await self.users.update_one({"id": user.id}, {"$set": fields})

    # ---------------- history ----------------
    async def log(self, uid, action, by=0, note=""):
        rec = await self.users.find_one({"id": int(uid)}, {"first_name": 1, "username": 1})
        name = ""
        if rec:
            name = rec.get("first_name") or (f"@{rec['username']}" if rec.get("username") else "")
        await self.history.insert_one(
            {"ts": int(time.time()), "uid": int(uid), "name": name, "action": action, "by": int(by), "note": note}
        )

    async def get_history(self, skip=0, limit=10, uid=None):
        q = {"uid": int(uid)} if uid else {}
        cur = self.history.find(q).sort("ts", -1).skip(skip).limit(limit)
        return await cur.to_list(limit), await self.history.count_documents(q)

    # ---------------- limits ----------------
    async def set_user_quota(self, uid, period, limit):
        """period=None clears the personal limit (falls back to the default)."""
        if period is None:
            await self.users.update_one({"id": int(uid)}, {"$unset": {"quota": ""}})
        else:
            await self.users.update_one(
                {"id": int(uid)}, {"$set": {"quota": {"period": period, "limit": limit}}}
            )

    async def set_default_quota(self, period, limit):
        await self.settings.update_one(
            {"_id": "quota"}, {"$set": {"period": period, "limit": limit}}, upsert=True
        )

    async def get_default_quota(self):
        d = await self.settings.find_one({"_id": "quota"})
        if not d or d.get("period") in (None, "unlimited"):
            return "unlimited", None
        return d["period"], d["limit"]

    async def effective_quota(self, uid):
        rec = await self.get_user(uid)
        if rec and rec.get("quota"):
            q = rec["quota"]
            if q["period"] == "unlimited":
                return "unlimited", None
            return q["period"], q["limit"]
        return await self.get_default_quota()

    async def record_usage(self, uid):
        now = int(time.time())
        await self.usage.insert_one({"uid": int(uid), "ts": now})
        await self.users.update_one({"id": int(uid)}, {"$set": {"last_used": now}})

    async def count_usage(self, uid, period):
        since = 0 if period == "life" else int(time.time()) - PERIODS[period]
        return await self.usage.count_documents({"uid": int(uid), "ts": {"$gte": since}})

    async def check_quota(self, uid):
        """Returns (allowed, message_if_blocked)."""
        if is_exempt(uid):
            return True, None
        period, limit = await self.effective_quota(uid)
        if period == "unlimited" or limit is None:
            return True, None
        used = await self.count_usage(uid, period)
        if used < limit:
            return True, None
        if period == "life":
            return False, f"⛔ You have used all {limit} of your links. Contact the admin for more."
        since = int(time.time()) - PERIODS[period]
        docs = await (
            self.usage.find({"uid": int(uid), "ts": {"$gte": since}})
            .sort("ts", 1).skip(used - limit).limit(1).to_list(1)
        )
        wait = docs[0]["ts"] + PERIODS[period] - int(time.time()) if docs else PERIODS[period]
        return False, (
            f"⛔ Limit reached: {limit} link(s) per {period}.\n"
            f"Try again in about {fmt_duration(wait)}."
        )

    # ---------------- access groups ----------------
    async def note_chat(self, chat, enabled=None):
        fields = {"title": chat.title or str(chat.id), "type": str(chat.type.name).lower(), "seen_at": int(time.time())}
        upd = {"$set": fields, "$setOnInsert": {"enabled": False}}
        if enabled is not None:
            upd = {"$set": {**fields, "enabled": enabled}}
        await self.chats.update_one({"chat_id": chat.id}, upd, upsert=True)
        self._group_cache["ts"] = 0

    async def forget_chat(self, chat_id):
        await self.chats.delete_one({"chat_id": chat_id})
        self._group_cache["ts"] = 0

    async def set_chat_enabled(self, chat_id, enabled):
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"enabled": enabled}})
        self._group_cache["ts"] = 0

    async def list_chats(self, enabled):
        return await self.chats.find({"enabled": enabled}).sort("title", 1).to_list(100)

    async def group_ids(self):
        """Chats whose members get access: USER_GROUP_ID (if it is a real chat id) + enabled DB chats."""
        if time.time() - self._group_cache["ts"] < 30:
            return self._group_cache["ids"]
        ids = [Var.USER_GROUP_ID] if Var.USER_GROUP_ID < 0 else []
        for c in await self.list_chats(True):
            if c["chat_id"] not in ids:
                ids.append(c["chat_id"])
        self._group_cache = {"ts": time.time(), "ids": ids}
        return ids

    async def chat_status(self, client, chat_id, uid):
        """ChatMemberStatus of uid in chat_id, or None. Positive results cached for 60s."""
        hit = self._status_cache.get((chat_id, uid))
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        try:
            status = (await client.get_chat_member(chat_id, uid)).status
        except UserNotParticipant:
            return None
        except PeerIdInvalid:
            # Users who message the bot are always known to it, so this means the bot cannot see the chat.
            if time.time() - self._warned.get(chat_id, 0) > 600:
                self._warned[chat_id] = time.time()
                logging.warning(
                    f"Bot cannot see access chat {chat_id} (Peer id invalid): add the bot to it as an admin. "
                    "Until then nobody is recognised as a member of that chat."
                )
            return None
        except Exception as e:
            logging.warning(f"get_chat_member({chat_id}, {uid}) failed: {e}")
            return None
        if status in MEMBER_STATUSES:
            self._status_cache[(chat_id, uid)] = (time.time(), status)
        return status

    async def chat_visibility(self, client, chat_id):
        """(ok, detail): can the bot see this chat, and in what role? Used by the admin menu."""
        try:
            me = await client.get_chat_member(chat_id, "me")
            return True, me.status.name.lower()
        except Exception as e:
            return False, type(e).__name__

    # ---------------- join requests ----------------
    async def add_join_request(self, uid):
        await self.join_requests.update_one(
            {"id": int(uid)},
            {"$set": {"id": int(uid), "requested_on": int(time.time())}},
            upsert=True,
        )

    async def has_join_request(self, uid):
        return bool(await self.join_requests.find_one({"id": int(uid)}))

    async def remove_join_request(self, uid):
        await self.join_requests.delete_many({"id": int(uid)})

    # ---------------- invites ----------------
    async def create_invite(self, by, target_id=None):
        token = secrets.token_urlsafe(8)
        await self.invites.insert_one(
            {"token": token, "by": int(by), "target_id": target_id, "used": False,
             "created_at": int(time.time()), "expires_at": int(time.time()) + INVITE_TTL}
        )
        return token

    async def redeem_invite(self, token, uid):
        """Atomically consume a valid invite. Returns the invite doc or None."""
        now = int(time.time())
        inv = await self.invites.find_one({"token": token})
        if not inv or inv["used"] or inv["expires_at"] < now:
            return None
        if inv.get("target_id") and inv["target_id"] != uid:
            return None
        return await self.invites.find_one_and_update(
            {"token": token, "used": False}, {"$set": {"used": True, "used_by": int(uid), "used_at": now}}
        )


access_db = AccessDB()


async def has_access(client, user_id):
    """Returns (allowed, reason) where reason is 'banned' / 'pending' / 'denied' when blocked.

    Order: owners/trusted -> explicit ban -> explicitly approved -> member of an access group
    -> has asked to join an access group.
    """
    if is_exempt(user_id):
        return True, None
    rec = await access_db.get_user(user_id)
    if rec and rec.get("status") == "banned":
        return False, "banned"
    if rec and rec.get("status") == "approved":
        return True, None
    banned_in_group = False
    for chat_id in await access_db.group_ids():
        status = await access_db.chat_status(client, chat_id, user_id)
        if status in MEMBER_STATUSES:
            return True, None
        if status == enums.ChatMemberStatus.BANNED:
            banned_in_group = True
    if banned_in_group:
        await access_db.remove_join_request(user_id)
    elif await access_db.has_join_request(user_id):
        return True, None
    return False, "pending" if rec and rec.get("status") == "pending" else "denied"


async def notify_owners(client, text, markup=None):
    """DM every owner. Failures (owner never started the bot) are logged, not raised."""
    for owner in Var.OWNER_ID:
        try:
            await client.send_message(
                owner, text, reply_markup=markup,
                parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True,
            )
        except Exception as e:
            logging.warning(f"Could not notify owner {owner}: {e}")


_sponsor_cache = {}


async def channel_sponsor(client, chat_id):
    """For a channel where the bot is admin: the id of a human admin of that channel who has access
    to the bot (that person is charged for usage), or None. Cached for 5 minutes.
    Channels with no such admin are ignored, so adding the bot to a channel is no way around approval."""
    hit = _sponsor_cache.get(chat_id)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    sponsor = None
    try:
        async for member in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            user = member.user
            if user and not user.is_bot and (await has_access(client, user.id))[0]:
                sponsor = user.id
                break
    except Exception as e:
        logging.warning(f"Could not list admins of channel {chat_id}: {e}")
    _sponsor_cache[chat_id] = (time.time(), sponsor)
    return sponsor
