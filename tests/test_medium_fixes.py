"""Offline regression tests for the medium-severity fixes (limits, deep links, hashes, admin menu ...).

Run:  python tests/test_medium_fixes.py   (or: pytest tests)
Telegram and MongoDB are replaced by in-memory fakes; dummy environment values are set before import.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
import asyncio
import subprocess
import time
import types

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _shared import LOOP  # noqa: E402
import tempfile as _tempfile
os.environ["LOG_DIR"] = _tempfile.mkdtemp(prefix="ftl-test-logs-")  # tests never write to the real logs/
os.environ.update(API_ID="1", API_HASH="x", BOT_TOKEN="1:x", BIN_CHANNEL="-100", OWNER_ID="111", TRUSTED_USERS="222",
                  USER_GROUP_ID="-1005", MONGO_SCHEMA="mongodb", MONGO_HOST="127.0.0.1", MONGO_USERNAME="u",
                  MONGO_PASSWORD="p", FQDN="example.com")
os.environ.pop("DYNO", None)

from pyrogram import StopPropagation
from pyrogram.errors import FloodWait, PeerIdInvalid


from _shared import FakeCol, Recorder, install_fakes, user  # noqa: E402


async def main():
    from _shared import quiet_database
    quiet_database()
    import streamgram.server  # same import order as the application (avoids a pre-existing import cycle)
    from streamgram.utils import access
    from streamgram.utils.access import access_db as db
    from streamgram.utils.file_properties import get_hash, hash_ok
    install_fakes(db)

    # ---- M1: a duplicate insert from elsewhere must not break touch()
    class RaceCol(FakeCol):
        async def find_one(self, q, proj=None): return None  # looked empty ...
    db.users = RaceCol("id"); await FakeCol.insert_one(db.users, {"id": 7, "status": "group"})  # ... but is there
    await db.touch(user(7))
    assert db.users.docs[0]["last_used"], "touch must fall back to an update"
    print("M1 ok: duplicate-key fallback in touch()")

    # ---- M3: burst of files cannot slip under the limit; refund gives it back
    install_fakes(db)
    await db.settings.insert_one({"_id": "quota", "period": "day", "limit": 3})
    res = await asyncio.gather(*[db.reserve(5) for _ in range(10)])
    assert sum(1 for ok, _, _ in res if ok) == 3 and len(db.usage.docs) == 3, res
    token = next(t for ok, _, t in res if ok)
    await db.refund(token)
    ok, msg, t2 = await db.reserve(5); assert ok and len(db.usage.docs) == 3
    ok, msg, _ = await db.reserve(5); assert not ok and "Limit reached" in msg
    assert (await db.reserve(111))[0] and (await db.reserve(222))[0]          # owner / trusted exempt
    print("M3 ok: exactly 3 of 10 simultaneous links allowed, refund works")

    # ---- plugins under test
    from streamgram.bot.plugins import access_admin as aa, start_help as sh, admin as adminmod
    from pyrogram import enums

    # ---- M2: database failure answers the user instead of dropping the message
    m = Recorder(from_user=user(5), text="hi")
    async def boom(*a, **k): raise RuntimeError("db down")
    real_has = sh.has_access; sh.has_access = boom
    try:
        await sh.check_user(Recorder(), m); raise SystemExit("StopPropagation expected")
    except StopPropagation:
        assert m.sent and "try again" in m.sent[0][0]
    sh.has_access = real_has
    async def ok_access(c, uid): return True, None
    sh.has_access = ok_access
    real_touch = db.touch; db.touch = boom
    m2 = Recorder(from_user=user(5), text="hi")
    assert await sh.check_user(Recorder(), m2) is None and not m2.sent       # touch failure never blocks
    db.touch = real_touch; sh.has_access = real_has
    print("M2 ok: DB failure -> friendly reply; bookkeeping failure ignored")

    # ---- password login is a standalone access path, independent of admin approval
    from streamgram.bot.plugins import stream as stm
    stm.MY_PASS = "secret"
    async def denied(c, uid): return False, None
    sh.has_access = denied
    class PD:
        async def get_user_pass(s, uid): return stm.pass_token("secret")
    real_pass_db = stm.pass_db; stm.pass_db = PD()
    m3 = Recorder(from_user=user(9), text="hi", chat=types.SimpleNamespace(id=9))
    assert await sh.check_user(Recorder(), m3) is None and not m3.sent        # logged-in -> bypasses admin approval
    async def banned(c, uid): return False, "banned"
    sh.has_access = banned
    m4b = Recorder(from_user=user(9), text="hi", chat=types.SimpleNamespace(id=9))
    try:
        await sh.check_user(Recorder(), m4b); raise SystemExit("StopPropagation expected")
    except StopPropagation:
        assert m4b.sent and "banned" in m4b.sent[0][0].lower()                # a ban still wins over password login
    sh.has_access = denied
    m5 = Recorder(from_user=user(9), text="/login", chat=types.SimpleNamespace(id=9))
    assert await sh.check_user(Recorder(), m5) is None and not m5.sent        # /login reachable before approval
    stm.login_waiting[9] = time.time() + 90
    m6 = Recorder(from_user=user(9), text="secret", chat=types.SimpleNamespace(id=9))
    assert await sh.check_user(Recorder(), m6) is None and not m6.sent        # the password reply too
    del stm.login_waiting[9]
    stm.MY_PASS = None; stm.pass_db = real_pass_db; sh.has_access = real_has
    print("password login ok: bypasses admin approval, a ban still wins, /login flow reachable")

    # ---- M4: cooldown after reject / revoke
    install_fakes(db)
    await db.users.insert_one({"id": 5, "status": "rejected", "updated_at": int(time.time())})
    client = Recorder()
    cq = Recorder(from_user=user(5), message=Recorder())
    await aa.request_access(client, cq)
    assert cq.answers and cq.answers[0][1] and "wait" in cq.answers[0][0]
    db.users.docs[0]["updated_at"] = int(time.time()) - 7200
    cq = Recorder(from_user=user(5), message=Recorder())
    await aa.request_access(client, cq)
    assert db.users.docs[0]["status"] == "pending" and client.sent       # owners were notified
    print("M4 ok: request cooldown")

    # ---- M5 + M8: revoke warns when a group still grants access; notification buttons replaced by a noop button
    install_fakes(db)
    await db.users.insert_one({"id": 55, "status": "approved", "first_name": "P", "updated_at": 1})
    async def via_group(c, uid): return True
    aa.group_access = via_group
    cq = Recorder(data="adm:rev:55", from_user=user(111, "Owner"), message=Recorder(text="detail"))
    await aa.on_action(Recorder(), cq)
    assert cq.answers[0][1] and "Ban" in cq.answers[0][0] and db.users.docs[0]["status"] == "revoked"
    assert cq.message.edits and "group" in cq.message.edits[-1][0].lower()   # detail page warns too
    await db.users.insert_one({"id": 56, "status": "pending", "first_name": "Q", "updated_at": 1})
    cq = Recorder(data="adm:apr:56", from_user=user(111, "Owner"), message=Recorder(text="🔔 Access request"))
    await aa.on_action(Recorder(), cq)
    kb = cq.message.edits[-1][1]["reply_markup"].inline_keyboard
    assert len(kb) == 1 and kb[0][0].callback_data == "adm:noop"
    print("M5/M8 ok: revoke warning + inert outcome button")

    # ---- M6: invite for an ID the bot has never seen -> link only that ID can use
    install_fakes(db)
    aa.StreamBot.username = "testbot"
    aa.inputs[111] = {"kind": "invite_user"}
    async def get_users(x): raise PeerIdInvalid("x")
    c = Recorder(); c.get_users = get_users
    m = Recorder(from_user=user(111), text="999000111")
    await aa.owner_input(c, m)
    assert db.invites.docs and db.invites.docs[0]["target_id"] == 999000111 and "t.me/testbot?start=inv_" in m.sent[0][0]
    assert 111 not in aa.inputs
    assert await db.redeem_invite(db.invites.docs[0]["token"], 5) is None            # other accounts can't use it
    assert await db.redeem_invite(db.invites.docs[0]["token"], 999000111) is not None
    print("M6 ok: invite link for unknown ID")

    # ---- M9: /start deep link needs <id>_<hash>
    install_fakes(db)
    async def _true(uid): return True
    sh.db = types.SimpleNamespace(is_user_exist=_true)
    sh.Var.UPDATES_CHANNEL = None
    media = types.SimpleNamespace(file_unique_id="AgADabcdefGHIJK", file_name="a<b>.zip", file_size=2048)
    async def get_messages(chat, mid):
        if mid == 9: return types.SimpleNamespace(empty=True)
        return types.SimpleNamespace(empty=False, document=media)
    b = Recorder(); b.get_messages = get_messages
    async def run(payload):
        m = Recorder(from_user=user(5), chat=types.SimpleNamespace(id=5), text=f"/start {payload}")
        await sh.start(b, m); return m.sent[0][0] if m.sent else None
    bad = "not valid"
    assert bad in await run("12")                 # bare id: no longer enough
    assert bad in await run("12_ZZZZZZ")          # wrong hash
    assert bad in await run("9_AgADab")           # no such message
    assert bad in await run("abc")
    ok = await run("12_AgADab"); assert "a&lt;b&gt;.zip" in ok and "/12/?hash=AgADab" in ok
    assert "/12/?hash=AgADabcdefGH" in await run("file_12_AgADabcdefGH")
    await db.set_revoked(12); assert bad in await run("12_AgADab")
    print("M9 ok: deep link requires a valid hash, escapes the name, honours revocation")

    # ---- M10: longer hashes, legacy 6-char links still work, revocation over HTTP
    uid_ = "AgADabcdefGHIJKLMNOP"
    msg = types.SimpleNamespace(document=types.SimpleNamespace(file_unique_id=uid_))
    assert get_hash(msg) == uid_[:12]
    assert hash_ok(uid_, uid_[:12]) and hash_ok(uid_, uid_[:6]) and not hash_ok(uid_, uid_[:5]) \
        and not hash_ok(uid_, "") and not hash_ok(uid_, None) and not hash_ok(uid_, "AgADabX")
    from fastapi.testclient import TestClient
    from streamgram.server import app, stream_routes as sr
    from streamgram.utils.custom_dl import ByteStreamer
    from streamgram.bot import multi_clients, work_loads
    blob = b"x" * 5000
    class Sess:
        async def send(self, req):
            from pyrogram import raw
            return raw.types.upload.File(type=None, mtime=0, bytes=blob[req.offset:req.offset + req.limit])
    bs = object.__new__(ByteStreamer); bs.client = None
    async def gms(cl, f): return Sess()
    async def gl(f): return None
    bs.generate_media_session = gms; ByteStreamer.get_location = staticmethod(gl)
    fid = types.SimpleNamespace(unique_id=uid_, file_size=len(blob), mime_type="video/mp4", file_name="v.mp4", file_type="FileType.DOCUMENT")
    async def gfp(self, mid): return fid
    ByteStreamer.get_file_properties = gfp
    multi_clients[0] = None; work_loads[0] = 0; sr.class_cache.clear(); sr.class_cache[None] = bs
    tc = TestClient(app, raise_server_exceptions=False)
    assert tc.get(f"/5?hash={uid_[:12]}").status_code == 200                 # new 12-char link
    assert tc.get(f"/5?hash={uid_[:6]}").status_code == 200                  # legacy link
    assert tc.get(f"/{uid_[:6]}5").status_code == 200                        # legacy <hash><id> form
    assert tc.get("/5?hash=AgADab9").status_code == 403
    await db.set_revoked(5)
    assert tc.get(f"/5?hash={uid_[:12]}").status_code == 404
    assert tc.get(f"/watch/5/?hash={uid_[:12]}").status_code == 404
    await db.set_revoked(5, revoked=False)
    assert tc.get(f"/5?hash={uid_[:12]}").status_code == 200
    # the owner commands
    m = Recorder(from_user=user(111), command=["revoke", "77"], text="/revoke 77")
    await aa.revoke_cmd(Recorder(), m); assert await db.is_revoked(77)
    m = Recorder(from_user=user(111), command=["unrevoke", "77"], text="/unrevoke 77")
    await aa.revoke_cmd(Recorder(), m); assert not await db.is_revoked(77)
    m = Recorder(from_user=user(111), command=["revoke"], text="/revoke"); await aa.revoke_cmd(Recorder(), m); assert "Usage" in m.sent[0][0]
    # watch page link no longer relies on the glued <hash><id> form
    from streamgram.utils import render_template as rt
    async def gfi(client, chat, mid): return types.SimpleNamespace(unique_id=uid_, file_name="v.mp4", mime_type="video/mp4", file_size=5)
    rt.get_file_ids = gfi
    page = await rt.render_page(7, uid_[:12])
    assert f"/7/?hash={uid_[:12]}" in page
    print("M10 ok: 12-char hashes, legacy links, revoke/unrevoke")

    # ---- M12: /broadcast needs a replied-to message; banned users are skipped
    install_fakes(db)
    m = Recorder(from_user=user(111), reply_to_message=None, text="/broadcast")
    await adminmod.broadcast_(Recorder(), m); assert "Reply to the message" in m.sent[0][0]
    await db.users.insert_one({"id": 1, "status": "banned"}); await db.users.insert_one({"id": 2, "status": "approved"})
    class Users:
        def __aiter__(self):
            async def g():
                for i in (1, 2, 3): yield {"id": i}
            return g()
    async def ret(v): return v
    fake_db = types.SimpleNamespace(get_all_users=lambda: ret(Users()), total_users_count=lambda: ret(3),
                                    delete_user=lambda i: ret(None))
    adminmod.db = fake_db
    got = []
    async def send_msg(user_id, message): got.append(user_id); return 200, None
    adminmod.send_msg = send_msg
    real_sleep = asyncio.sleep
    async def fast_sleep(*a, **k): return await real_sleep(0)
    adminmod.asyncio.sleep = fast_sleep
    try:
        m = Recorder(from_user=user(111), reply_to_message=object(), text="/broadcast")
        await adminmod.broadcast_(Recorder(), m)
    finally:
        adminmod.asyncio.sleep = real_sleep
    assert got == [2, 3] and "1 banned skipped" in m.sent[-1][0], (got, m.sent)
    print("M12 ok: broadcast guard + banned skipped")

    # ---- M13: FloodWait uses .value and the retry is awaited
    from streamgram.utils.broadcast_helper import send_msg as real_send
    import streamgram.utils.broadcast_helper as bh
    calls = []
    class Msg:
        async def forward(self, chat_id):
            calls.append(chat_id)
            if len(calls) == 1: raise FloodWait(value=2)
    slept = []
    async def fs(n): slept.append(n)
    bh.asyncio.sleep = fs
    try:
        assert await real_send(9, Msg()) == (200, None)
    finally:
        bh.asyncio.sleep = real_sleep
    assert slept == [2] and calls == [9, 9]
    print("M13 ok: FloodWait.value + awaited retry")

    # ---- M11: TRUSTED_USERS / OWNER_ID parsing
    def parse(trusted):
        env = dict(os.environ, TRUSTED_USERS=trusted)
        out = subprocess.run([sys.executable, "-c", "from streamgram.vars import Var; print(sorted(Var.TRUSTED_USERS))"],
                             env=env, cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[-300:]
        return out.stdout.strip()
    assert parse("1  2,3 ") == "[1, 2, 3]" and parse("") == "[]" and parse(" 7 ") == "[7]"
    print("M11 ok: tolerant TRUSTED_USERS parsing")

    # ---- blank optional numeric vars (e.g. `USER_GROUP_ID=` in config.env, exactly what
    # config.env.example documents for an unused one) must fall back to the default rather
    # than crash. Found live: int(getenv(name, default)) only substitutes the default when
    # the variable is completely absent, not when it's present-but-empty; a real docker
    # compose run with USER_GROUP_ID= crashed the whole process before this was fixed.
    def user_group_id(value_or_none):
        env = dict(os.environ)
        if value_or_none is None:
            env.pop("USER_GROUP_ID", None)
        else:
            env["USER_GROUP_ID"] = value_or_none
        out = subprocess.run([sys.executable, "-c", "from streamgram.vars import Var; print(Var.USER_GROUP_ID)"],
                             env=env, cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[-300:]
        return out.stdout.strip()
    assert user_group_id(None) == "1" and user_group_id("") == "1" and user_group_id("-1005") == "-1005"
    print("blank USER_GROUP_ID ok: falls back to the default instead of crashing")

    # ---- get_mongo_uri() with no username/password (an authless local/dev Mongo, e.g. a
    # plain `mongo` container) must omit the user:pass@ segment entirely, not embed empty
    # credentials. Found live: embedding ":@" unconditionally produced "mongodb://:@host:port/",
    # which pymongo rejects outright (InvalidURI: "The empty string is not valid username").
    from streamgram.utils.database import get_mongo_uri
    import streamgram.vars as vars_mod
    saved = (vars_mod.Var.MONGO_USERNAME, vars_mod.Var.MONGO_PASSWORD)
    try:
        vars_mod.Var.MONGO_USERNAME, vars_mod.Var.MONGO_PASSWORD = "", ""
        assert get_mongo_uri() == "mongodb://127.0.0.1:27017/"
        vars_mod.Var.MONGO_USERNAME, vars_mod.Var.MONGO_PASSWORD = "u", "p"
        assert get_mongo_uri() == "mongodb://u:p@127.0.0.1:27017/"
    finally:
        vars_mod.Var.MONGO_USERNAME, vars_mod.Var.MONGO_PASSWORD = saved
    print("get_mongo_uri ok: no empty user:pass@ segment when credentials are blank")


def test_medium_fixes():
    LOOP.run_until_complete(main())


if __name__ == "__main__":
    test_medium_fixes()
    print("ALL MEDIUM FIXES PASS")
