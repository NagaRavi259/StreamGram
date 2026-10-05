"""Helpers shared by the test modules: one event loop (the plugins keep database clients bound to it) and
in-memory stand-ins for MongoDB / Telegram objects."""
import asyncio
import types
from pymongo.errors import DuplicateKeyError
from pyrogram.errors import UserNotParticipant


LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


# ---------------------------------------------------------------- in-memory MongoDB stand-in
class Cursor:
    def __init__(self, docs): self.docs = list(docs)

    def sort(self, key, direction=1):
        keys = key if isinstance(key, list) else [(key, direction)]
        for field, d in reversed(keys):
            self.docs.sort(key=lambda x: (x.get(field) is not None, x.get(field) or 0), reverse=d < 0)
        return self

    def skip(self, n): self.docs = self.docs[n:]; return self
    def limit(self, n): self.docs = self.docs[:n]; return self
    async def to_list(self, n): return self.docs[:n]

    def __aiter__(self):
        async def gen():
            for d in self.docs: yield d
        return gen()


class FakeCol:
    def __init__(self, unique=None): self.docs, self.n, self.unique = [], 0, unique

    @staticmethod
    def _match(d, q):
        for k, v in q.items():
            val = d.get(k)
            if isinstance(v, dict):
                for op, x in v.items():
                    if op == "$in" and val not in x: return False
                    if op == "$gte" and not (val is not None and val >= x): return False
            elif val != v:
                return False
        return True

    def _all(self, q): return [d for d in self.docs if self._match(d, q or {})]
    async def find_one(self, q, proj=None): r = self._all(q); return dict(r[0]) if r else None
    def find(self, q=None, proj=None): return Cursor(dict(d) for d in self._all(q))
    async def count_documents(self, q): return len(self._all(q))

    async def insert_one(self, d):
        if self.unique and any(x.get(self.unique) == d.get(self.unique) for x in self.docs):
            raise DuplicateKeyError("dup")
        self.n += 1; d = dict(d); d.setdefault("_id", self.n); self.docs.append(d)
        return types.SimpleNamespace(inserted_id=d["_id"])

    async def update_one(self, q, u, upsert=False):
        r = self._all(q)
        if not r and upsert:
            doc = {k: v for k, v in q.items() if not isinstance(v, dict)}
            doc.update(u.get("$setOnInsert", {})); await self.insert_one(doc); r = [self.docs[-1]]
        for d in r[:1]:
            d.update(u.get("$set", {}))
            for k in u.get("$unset", {}): d.pop(k, None)

    async def create_index(self, *a, **k): return None

    async def find_one_and_update(self, q, u):
        r = self._all(q)
        if not r: return None
        before = dict(r[0]); r[0].update(u.get("$set", {})); return before

    async def delete_one(self, q):
        r = self._all(q)
        if r: self.docs.remove(r[0])

    delete_many = delete_one


def install_fakes(db):
    for name in [n for n in vars(db) if hasattr(type(db), n)]:
        delattr(db, name)  # drop methods another test module patched on the shared instance
    db.users, db.usage, db.history = FakeCol("id"), FakeCol(), FakeCol()
    db.join_requests, db.settings, db.revoked = FakeCol(), FakeCol(), FakeCol()
    db.invites, db.chats, db.links = FakeCol(), FakeCol(), FakeCol()
    db.link_tokens = FakeCol("msg_id")
    from streamgram.utils.link_expiry import link_expiry
    from streamgram.utils.link_security import link_tokens
    link_expiry._cache.clear()
    link_tokens._cache.clear()
    db._touched.clear(); db._locks.clear(); db._status_cache.clear(); db._revoked = {"ts": 0, "ids": set()}


class Recorder:
    """Stands in for a Telegram message / callback / client; remembers what was sent."""
    def __init__(self, **kw):
        self.sent, self.answers, self.edits = [], [], []
        self.__dict__.update(kw)

    async def reply_text(self, text, **k): self.sent.append((text, k)); return self
    async def reply_photo(self, photo, **k): self.sent.append((k.get("caption"), {"photo": photo, **k})); return self
    async def send_message(self, chat=None, text=None, chat_id=None, **k): self.sent.append((text, k))
    async def answer(self, text=None, show_alert=False, **k): self.answers.append((text, show_alert))
    async def edit_text(self, text, **k): self.edits.append((text, k))
    async def delete(self): pass
    async def get_chat_member(self, chat, who): raise UserNotParticipant("not a member (default test stand-in)")


def user(uid, name="N"): return types.SimpleNamespace(id=uid, first_name=name, username=None, is_bot=False)


def quiet_database():
    """Plugins connect to MongoDB at import time; in tests there is no database, so skip the startup ping."""
    from streamgram.utils.database import Database

    async def noop(self):
        return None
    Database.initialize = noop
