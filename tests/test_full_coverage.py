"""Regression tests for the pieces not already exercised by test_high_fixes.py,
test_medium_fixes.py and test_expiry_logging_low.py: the pure formatting helpers
(humanbytes, get_readable_time), MULTI_TOKEN_* parsing and multi-client startup,
the keep-alive ping, the /start /help /about branding handlers, and the admin-menu
screens for groups, limits, history and the users/pending lists.

Run:  python tests/test_full_coverage.py   (or: pytest tests)
"""
import asyncio
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _shared import LOOP  # noqa: E402
import tempfile as _tempfile
os.environ["LOG_DIR"] = _tempfile.mkdtemp(prefix="ftl-test-logs-")  # tests never write to the real logs/
os.environ.update(API_ID="1", API_HASH="x", BOT_TOKEN="1:x", BIN_CHANNEL="-100", OWNER_ID="111", TRUSTED_USERS="222",
                  USER_GROUP_ID="-1005", MONGO_SCHEMA="mongodb", MONGO_HOST="127.0.0.1", MONGO_USERNAME="u",
                  MONGO_PASSWORD="p", FQDN="example.com")
os.environ.pop("DYNO", None)
os.environ.pop("MULTI_TOKEN_1", None)
os.environ.pop("MULTI_TOKEN_2", None)

from pyrogram import enums
from pyrogram.errors import PeerIdInvalid, UserNotParticipant

from _shared import FakeCol, Recorder, install_fakes, user, quiet_database  # noqa: E402


def all_urls(markup):
    return [b.url for row in markup.inline_keyboard for b in row if getattr(b, "url", None)]


async def main():
    quiet_database()
    import streamgram.server  # same import order as the application (avoids a pre-existing import cycle)
    from streamgram.utils.access import access_db as db
    install_fakes(db)

    # ================= pure helpers =================
    from streamgram.utils.human_readable import humanbytes
    assert humanbytes(0) == "" and humanbytes(None) == "" and humanbytes("") == ""
    assert humanbytes(500) == "500 B"
    assert humanbytes(1024) == "1.0 KiB"             # N1 regression: used to stop one unit short
    assert humanbytes(1025) == "1.0 KiB"
    assert humanbytes(1024 * 1024) == "1.0 MiB"
    assert humanbytes(1024 ** 3) == "1.0 GiB"
    assert humanbytes(1024 ** 4) == "1.0 TiB"
    assert humanbytes(123456789) == "117.74 MiB"
    assert humanbytes(1024 ** 5) == "1024.0 TiB"      # beyond Ti there's no further unit, by design
    print("humanbytes ok (incl. the exact-power-of-1024 case, N1 fixed)")

    from streamgram.utils.time_format import get_readable_time
    assert get_readable_time(0) == ""
    assert get_readable_time(5) == "5s"
    assert get_readable_time(65) == "1m: 5s"
    assert get_readable_time(3661) == "1h: 1m: 1s"
    assert get_readable_time(90061) == "1 days, 1h: 1m: 1s"
    print("get_readable_time ok")

    from streamgram.utils.config_parser import TokenParser
    for k in list(os.environ):
        if k.startswith("MULTI_TOKEN"):
            del os.environ[k]
    assert TokenParser().parse_from_env() == {}
    os.environ["MULTI_TOKEN_2"] = "tok2"
    os.environ["MULTI_TOKEN_1"] = "tok1"
    assert TokenParser().parse_from_env() == {1: "tok1", 2: "tok2"}  # renumbered 1..n in sorted env-key order
    del os.environ["MULTI_TOKEN_1"]; del os.environ["MULTI_TOKEN_2"]
    print("TokenParser ok")

    # ================= multi-client startup =================
    from streamgram.bot import clients as cl, multi_clients, work_loads, StreamBot
    multi_clients.clear(); work_loads.clear()
    await cl.initialize_clients()
    assert multi_clients == {0: StreamBot} and work_loads == {0: 0} and cl.Var.MULTI_CLIENT is False
    print("initialize_clients ok: no MULTI_TOKEN_* -> default client only")

    class FakeClient:
        instances = []
        def __init__(self, **kw): self.kw = kw; FakeClient.instances.append(self)
        async def start(self): return self
    real_client_cls = cl.Client
    cl.Client = FakeClient
    multi_clients.clear(); work_loads.clear()
    os.environ["MULTI_TOKEN_1"] = "tok1"; os.environ["MULTI_TOKEN_2"] = "tok2"
    await cl.initialize_clients()
    assert set(multi_clients) == {0, 1, 2} and set(work_loads) == {0, 1, 2} and cl.Var.MULTI_CLIENT is True
    assert {c.kw["bot_token"] for c in FakeClient.instances} == {"tok1", "tok2"}
    assert all(c.kw.get("no_updates") is True for c in FakeClient.instances)
    del os.environ["MULTI_TOKEN_1"]; del os.environ["MULTI_TOKEN_2"]
    cl.Client = real_client_cls; cl.Var.MULTI_CLIENT = False
    print("initialize_clients ok: extra MULTI_TOKEN_* clients started, MULTI_CLIENT flag set")

    class FlakyClient:
        def __init__(self, **kw): self.kw = kw
        async def start(self):
            if self.kw["bot_token"] == "bad":
                raise RuntimeError("could not log in")
            return self
    cl.Client = FlakyClient
    multi_clients.clear(); work_loads.clear()
    os.environ["MULTI_TOKEN_1"] = "bad"; os.environ["MULTI_TOKEN_2"] = "tok2"
    await cl.initialize_clients()                                    # N2 regression: used to crash here
    assert set(multi_clients) == {0, 2} and set(work_loads) == {0, 2}  # the failed client 1 is just skipped
    del os.environ["MULTI_TOKEN_1"]; del os.environ["MULTI_TOKEN_2"]
    cl.Client = real_client_cls; cl.Var.MULTI_CLIENT = False
    print("initialize_clients ok: one failed MULTI_TOKEN_* client is skipped, not fatal (N2 fixed)")

    # ================= keep-alive ping =================
    import streamgram.utils.keepalive as ka

    class FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class FakeSession:
        calls = []
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def get(self, url): FakeSession.calls.append(url); return FakeResp()

    real_session_cls = ka.aiohttp.ClientSession
    ka.aiohttp.ClientSession = FakeSession
    ka.Var.PING_INTERVAL = 0.01
    try:
        await asyncio.wait_for(ka.ping_server(), timeout=0.05)
    except asyncio.TimeoutError:
        pass
    assert FakeSession.calls, "ping_server should have pinged Var.URL at least once"
    ka.aiohttp.ClientSession = real_session_cls
    print("ping_server ok: pings Var.URL on a timer")

    # ================= /start /help /about branding =================
    from streamgram.bot.plugins import start_help as sh

    class FakeUserDB:
        def __init__(self): self.existing, self.added = set(), []
        async def is_user_exist(self, uid): return uid in self.existing
        async def add_user(self, uid): self.added.append(uid); self.existing.add(uid)

    sh.db = FakeUserDB()
    sh.Var.UPDATES_CHANNEL = None

    m = Recorder(from_user=user(50, "New"), chat=types.SimpleNamespace(id=50), text="/start")
    await sh.start(Recorder(), m)
    assert sh.db.added == [50]
    cap, kw = m.sent[-1]
    assert "I'm StreamGram" in cap
    print("/start ok: new user welcomed")

    sh.Var.UPDATES_CHANNEL = "mychan"

    class BotBanned(Recorder):
        async def get_chat_member(self, chan, uid): return types.SimpleNamespace(status="banned")
    b, m = BotBanned(), Recorder(from_user=user(51), chat=types.SimpleNamespace(id=51), text="/start")
    await sh.start(b, m)
    assert "ʙᴀɴɴᴇᴅ" in b.sent[-1][0]

    class BotNotMember(Recorder):
        async def get_chat_member(self, chan, uid): raise UserNotParticipant("x")
    b, m = BotNotMember(), Recorder(from_user=user(52), chat=types.SimpleNamespace(id=52), text="/start")
    await sh.start(b, m)
    text, kw = b.sent[-1]
    assert "ᴊᴏɪɴ ᴍʏ ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ" in text
    assert f"https://t.me/{sh.Var.UPDATES_CHANNEL}" in all_urls(kw["reply_markup"])

    class BotOddError(Recorder):
        async def get_chat_member(self, chan, uid): raise RuntimeError("boom")
    b, m = BotOddError(), Recorder(from_user=user(53), chat=types.SimpleNamespace(id=53), text="/start")
    await sh.start(b, m)
    assert "Welcome to the Ultimate Test Bot" in b.sent[-1][0]
    print("/start ok: UPDATES_CHANNEL banned / not-participant / other-error branches")

    sh.Var.UPDATES_CHANNEL = None
    m = Recorder(from_user=user(54), chat=types.SimpleNamespace(id=54), text="/help")
    await sh.help_handler(Recorder(), m)
    assert sh.db.added[-1] == 54
    cap, kw = m.sent[-1]
    assert "also works in channels" in cap
    print("/help ok")

    m = Recorder(from_user=user(55), chat=types.SimpleNamespace(id=55), text="/about")
    await sh.about_handler(Recorder(), m)
    cap, kw = m.sent[-1]
    assert "About this bot" in cap
    assert "github.com/NagaRavi259/StreamGram" in cap
    print("/about ok")

    # ================= B4: the shared force-subscribe helper =================
    from streamgram.utils.force_subscribe import enforce_updates_channel

    sh.Var.UPDATES_CHANNEL = None
    assert await enforce_updates_channel(Recorder(), 1) is True        # disabled: always allowed, no reply

    sh.Var.UPDATES_CHANNEL = "mychan"

    class UCBanned(Recorder):
        async def get_chat_member(self, chan, uid): return types.SimpleNamespace(status="banned")
    b = UCBanned()
    assert await enforce_updates_channel(b, 1) is False and "ʙᴀɴɴᴇᴅ" in b.sent[-1][0]

    class UCNotMember(Recorder):
        async def get_chat_member(self, chan, uid): raise UserNotParticipant("x")
    b = UCNotMember()
    assert await enforce_updates_channel(b, 1) is False
    assert "https://t.me/mychan" in all_urls(b.sent[-1][1]["reply_markup"])

    class UCOddError(Recorder):
        async def get_chat_member(self, chan, uid): raise RuntimeError("boom")
    b = UCOddError()
    assert await enforce_updates_channel(b, 1) is False and "Welcome to the Ultimate Test Bot" in b.sent[-1][0]
    sh.Var.UPDATES_CHANNEL = None
    print("enforce_updates_channel ok: disabled / banned / not-member / other-error")

    # stream.private_receive_handler now defers to the same helper instead of its own copy
    from streamgram.bot.plugins import stream as stm
    stm.Var.UPDATES_CHANNEL = "mychan"
    stm.db = FakeUserDB()

    class StreamBotBanned(UCBanned):
        pass
    b, m = StreamBotBanned(), Recorder(from_user=user(60), chat=types.SimpleNamespace(id=60))
    await stm.private_receive_handler(b, m)
    assert "ʙᴀɴɴᴇᴅ" in b.sent[-1][0] and not m.sent                       # stopped before reserving a quota slot
    stm.Var.UPDATES_CHANNEL = None
    print("private_receive_handler ok: reuses enforce_updates_channel (B4)")

    # ================= C2: stronger per-link tokens =================
    from streamgram.utils.link_security import link_tokens
    from streamgram.utils.file_properties import get_hash

    unique_id = "AgADabcdefGHIJKLMNOP"
    legacy_hash = get_hash(types.SimpleNamespace(document=types.SimpleNamespace(file_unique_id=unique_id)))

    # A link that predates tokenisation (nothing issued for it) still validates the old way.
    assert await link_tokens.check(901, unique_id, legacy_hash) is True
    assert await link_tokens.check(901, unique_id, "wrong") is False

    # A newly issued token is not derived from (or equal to) the old file_unique_id hash...
    token = await link_tokens.issue(902)
    assert token != legacy_hash and len(token) >= 12
    # ...and once a token exists for a message, only that exact token validates — the old
    # file_unique_id-derived hash no longer works for it, even though it's the same file.
    assert await link_tokens.check(902, unique_id, token) is True
    assert await link_tokens.check(902, unique_id, legacy_hash) is False
    assert await link_tokens.check(902, unique_id, token + "x") is False
    print("link_tokens ok: new links get an unguessable token; legacy links still fall back")

    # Links created by private_receive_handler now carry an issued token, not get_hash(log_msg).
    stm.Var.UPDATES_CHANNEL = None
    stm.db = FakeUserDB()
    doc = types.SimpleNamespace(file_unique_id=unique_id, file_name="f.zip", file_size=2048)
    log_msg = Recorder(id=903, document=doc)
    async def forward(chat_id): return log_msg
    msg = Recorder(from_user=user(61), chat=types.SimpleNamespace(id=61), document=doc)
    msg.forward = forward
    await stm.private_receive_handler(Recorder(), msg)
    sent_text = msg.sent[-1][0]
    issued = await link_tokens._get(903)
    assert issued and f"?hash={issued}" in sent_text and legacy_hash not in sent_text
    print("private_receive_handler ok: new links carry the issued token, not the old hash (C2)")

    # ================= admin menu: screens not covered elsewhere =================
    from streamgram.bot.plugins import access_admin as aa

    def cq_for(data, uid=111, name="Owner", msg_text="x"):
        return Recorder(data=data, from_user=user(uid, name), message=Recorder(text=msg_text))

    # ---- pager()
    assert aa.pager("p", 0, 5) == []                                    # fits on one page: no buttons
    only_next = aa.pager("p", 0, 20)
    assert len(only_next) == 1 and len(only_next[0]) == 1 and only_next[0][0].callback_data == "p:1"
    both = aa.pager("p", 1, 20)
    assert len(both[0]) == 2 and both[0][0].callback_data == "p:0" and both[0][1].callback_data == "p:2"
    only_prev = aa.pager("p", 2, 20)                                     # page 2 of 20 @ 8/page: no next
    assert len(only_prev[0]) == 1 and only_prev[0][0].callback_data == "p:1"
    print("pager ok")

    # ---- home / close
    install_fakes(db)
    m = Recorder(from_user=user(111), text="/admin")
    await aa.admin_cmd(Recorder(), m)
    assert "Admin menu" in m.sent[-1][0]
    cq = cq_for("adm:home")
    await aa.cb_home(Recorder(), cq)
    assert "Admin menu" in cq.message.edits[-1][0]

    class TrackDelete(Recorder):
        def __init__(self, **kw): super().__init__(**kw); self.deleted = False
        async def delete(self): self.deleted = True
    cq = Recorder(data="adm:close", from_user=user(111), message=TrackDelete(text="x"))
    await aa.cb_close(Recorder(), cq)
    assert cq.message.deleted
    print("admin_cmd / cb_home / cb_close ok")

    # ---- users list & pending list pagination (users_view / pager together)
    install_fakes(db)
    for i in range(10):
        await db.users.insert_one({"id": 1000 + i, "status": "approved", "first_name": f"U{i}", "last_used": 1000 + i})
    cq = cq_for("adm:users:0")
    await aa.cb_users(Recorder(), cq)
    text0, kw0 = cq.message.edits[-1]
    assert "10" in text0
    rows0 = kw0["reply_markup"].inline_keyboard
    assert len(rows0) == aa.PER_PAGE + 1 + 1                             # 8 users + pager (Next only) + home
    cq2 = cq_for("adm:users:1")
    await aa.cb_users(Recorder(), cq2)
    rows1 = cq2.message.edits[-1][1]["reply_markup"].inline_keyboard
    assert len(rows1) == 2 + 1 + 1                                       # remaining 2 users + pager (Prev only) + home

    await db.users.insert_one({"id": 2000, "status": "pending", "first_name": "Pend", "requested_at": 5})
    cq3 = cq_for("adm:pend:0")
    await aa.cb_pending(Recorder(), cq3)
    text3 = cq3.message.edits[-1][0]
    assert "<b>1</b>" in text3 and "Pending requests" in text3
    print("users/pending pagination ok")

    # ---- user detail screen
    cq4 = cq_for("adm:u:1000")
    await aa.cb_user(Recorder(), cq4)
    assert "1000" in cq4.message.edits[-1][0]
    print("show_user ok")

    # ---- default & per-user limits (cb_quota / cb_quota_period / owner_input "quota")
    install_fakes(db)
    cq = cq_for("adm:q:0")
    await aa.cb_quota(Recorder(), cq)
    assert "unlimited" in cq.message.edits[-1][0].lower()

    cq = cq_for("adm:qp:0:day")
    await aa.cb_quota_period(Recorder(), cq)
    assert aa.inputs[111] == {"kind": "quota", "uid": 0, "period": "day"}

    m = Recorder(from_user=user(111), text="abc")                        # rejected: not a number
    await aa.owner_input(Recorder(), m)
    assert "whole number" in m.sent[-1][0] and aa.inputs[111]["kind"] == "quota"

    m = Recorder(from_user=user(111), text="20")
    await aa.owner_input(Recorder(), m)
    assert await db.get_default_quota() == ("day", 20) and 111 not in aa.inputs

    cq = cq_for("adm:qp:0:unlimited")
    await aa.cb_quota_period(Recorder(), cq)
    assert await db.get_default_quota() == ("unlimited", None)

    await db.users.insert_one({"id": 77, "status": "approved"})
    cq = cq_for("adm:qp:77:life")
    await aa.cb_quota_period(Recorder(), cq)
    m = Recorder(from_user=user(111), text="5")
    await aa.owner_input(Recorder(), m)
    assert await db.effective_quota(77) == ("life", 5)
    cq = cq_for("adm:qp:77:default")                                     # "use default" drops the personal limit
    await aa.cb_quota_period(Recorder(), cq)
    assert await db.effective_quota(77) == ("unlimited", None)
    print("limits ok: default + per-user, numeric input validated, 'use default' clears override")

    # ---- history (global and per-user)
    install_fakes(db)
    await db.log(0, "system_note", 111, "note-one")
    await db.log(80, "approved", 111, "note-two")
    cq = cq_for("adm:hist:0")
    await aa.cb_history(Recorder(), cq)
    hist_text = cq.message.edits[-1][0]
    assert "system_note" in hist_text and "(system)" in hist_text    # uid 0 shown as "(system)"
    assert "approved" in hist_text and "<code>80</code>" in hist_text
    cq2 = cq_for("adm:uh:80:0")
    await aa.cb_user_history(Recorder(), cq2)
    assert "approved" in cq2.message.edits[-1][0] and "system_note" not in cq2.message.edits[-1][0]
    print("history ok: global + filtered by user")

    # ---- access groups: config line, pick-from-known, enable, remove, manual add by ID
    install_fakes(db)
    aa.Var.USER_GROUP_ID = -1005
    cq = cq_for("adm:grp")
    await aa.cb_groups(Recorder(), cq)
    assert "-1005" in cq.message.edits[-1][0]

    await db.note_chat(types.SimpleNamespace(id=-2002, title="Test Group", type=enums.ChatType.SUPERGROUP))
    cq = cq_for("adm:gadd")
    await aa.cb_group_pick(Recorder(), cq)
    labels = [b.text for row in cq.message.edits[-1][1]["reply_markup"].inline_keyboard for b in row]
    assert any("Test Group" in l for l in labels)

    cq = cq_for("adm:gp:-2002")
    await aa.cb_group_enable(Recorder(), cq)
    assert cq.answers[-1][0] == "Group added ✅"
    assert any(c["chat_id"] == -2002 for c in await db.list_chats(True))

    cq = cq_for("adm:grm:-2002")
    await aa.cb_group_remove(Recorder(), cq)
    assert cq.answers[-1][0] == "Group removed"
    assert not any(c["chat_id"] == -2002 for c in await db.list_chats(True))

    aa.inputs[111] = {"kind": "group_id"}
    class FakeGroupClient(Recorder):
        async def get_chat(self, x):
            return types.SimpleNamespace(id=-3003, title="Manual Grp", type=enums.ChatType.GROUP)
        async def get_chat_member(self, chat_id, who): return types.SimpleNamespace(status="member")
    m = Recorder(from_user=user(111), text="-3003")
    await aa.owner_input(FakeGroupClient(), m)
    assert any(c["chat_id"] == -3003 for c in await db.list_chats(True)) and 111 not in aa.inputs
    print("groups ok: config chat shown, pick/enable/remove, manual add by ID")

    # ---- invites: accept callback, and a ban blocking redemption
    install_fakes(db)
    await db.users.insert_one({"id": 90, "status": "banned"})
    assert await aa.redeem(Recorder(), user(90), "whatever-token") is False   # banned: never even checks the token

    token = await db.create_invite(111, 95)
    cq = Recorder(data=f"inv:acc:{token}", from_user=user(95), message=Recorder(text="x"))
    await aa.accept_invite(Recorder(), cq)
    assert cq.answers[-1][0] == "Access granted ✅"
    assert (await db.get_user(95))["status"] == "approved"
    print("invites ok: accept callback approves; a ban blocks redemption")

    # ---- chat tracking: added/removed as admin, fallback group-sighting, join requests
    install_fakes(db)
    me_admin = types.SimpleNamespace(user=types.SimpleNamespace(is_self=True), status=enums.ChatMemberStatus.ADMINISTRATOR)
    me_left = types.SimpleNamespace(user=types.SimpleNamespace(is_self=True), status=enums.ChatMemberStatus.LEFT)
    chat = types.SimpleNamespace(id=-4004, title="NewChat", type=enums.ChatType.SUPERGROUP)
    await aa.track_bot_chats(Recorder(), types.SimpleNamespace(new_chat_member=me_admin, old_chat_member=None, chat=chat))
    assert any(c["chat_id"] == -4004 for c in await db.chats.find({}).to_list(10))
    await aa.track_bot_chats(Recorder(), types.SimpleNamespace(new_chat_member=me_left, old_chat_member=me_admin, chat=chat))
    assert not any(c["chat_id"] == -4004 for c in await db.chats.find({}).to_list(10))

    aa._seen_chats.clear()
    seen_chat = types.SimpleNamespace(id=-5005, title="SeenGrp", type=enums.ChatType.GROUP)
    await aa.see_group(Recorder(), Recorder(chat=seen_chat))
    assert any(c["chat_id"] == -5005 for c in await db.chats.find({}).to_list(10))

    await aa.on_join_request(Recorder(), types.SimpleNamespace(from_user=user(96), chat=types.SimpleNamespace(id=-5005)))
    assert await db.has_join_request(96)
    print("chat tracking ok: note/forget on membership change, fallback sighting, join requests recorded")

    print("ALL FULL-COVERAGE CHECKS PASS")


if __name__ == "__main__":
    LOOP.run_until_complete(main())
else:
    def test_main():
        LOOP.run_until_complete(main())
