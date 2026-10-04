"""Offline regression tests for the streaming, templating, channel-gate and login fixes.

Run from anywhere:  python tests/test_high_fixes.py   (or: pytest tests)
No network and no real configuration are used: dummy environment values are set before the
package is imported, and Telegram / MongoDB access is replaced by in-memory fakes.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)  # templates and logs/ are resolved relative to the repository root
import asyncio, random, types
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _shared import LOOP  # noqa: E402
import tempfile as _tempfile
os.environ["LOG_DIR"] = _tempfile.mkdtemp(prefix="ftl-test-logs-")  # tests never write to the real logs/
os.environ.update(API_ID="1", API_HASH="x", BOT_TOKEN="1:x", BIN_CHANNEL="-100", OWNER_ID="111", TRUSTED_USERS="222",
                  USER_GROUP_ID="-1005", MONGO_SCHEMA="mongodb", MONGO_HOST="127.0.0.1", MONGO_USERNAME="u", MONGO_PASSWORD="p",
                  FQDN="example.com")
os.environ.pop("DYNO", None)
from pyrogram import raw, enums
from fastapi.testclient import TestClient
from fastapi import HTTPException

async def main():
    from _shared import quiet_database
    quiet_database()
    import streamgram.server
    from _shared import install_fakes
    from streamgram.utils.access import access_db as _access_db
    install_fakes(_access_db)
    from streamgram.server import stream_routes as sr
    from streamgram.utils import custom_dl, render_template
    from streamgram.utils.custom_dl import ByteStreamer, chunk_size, offset_fix
    from streamgram.bot import work_loads, multi_clients, StreamBot

    # ---------- H1: real yield_file + the same math as media_streamer, against real slices
    random.seed(1)
    blob = bytes(random.getrandbits(8) for _ in range(3_000_000))
    class Sess:
        async def send(self, req):
            return raw.types.upload.File(type=None, mtime=0, bytes=blob[req.offset:req.offset + req.limit])
    bs = object.__new__(ByteStreamer); bs.client = None
    async def gms(c, f): return Sess()
    async def gl(f): return None
    bs.generate_media_session = gms; ByteStreamer.get_location = staticmethod(gl)
    work_loads[0] = 0
    async def fetch(a, b):
        n = b - a + 1; cs = await chunk_size(n); off = await offset_fix(a, cs)
        parts = b // cs - off // cs + 1
        out = b"".join([c async for c in bs.yield_file(None, 0, off, a - off, b % cs + 1, parts, cs)])
        return out
    cases = [(0, len(blob)-1), (1_000_000, len(blob)-1), (1_000_000, 1_100_000), (131071, 131072), (0, 0),
             (len(blob)-1, len(blob)-1), (4095, 4096), (5, 5), (0, 4095), (0, 4096), (999_999, 2_000_000)]
    cases += [tuple(sorted((random.randrange(len(blob)), random.randrange(len(blob))))) for _ in range(300)]
    bad = [(a, b) for a, b in cases if await fetch(a, b) != blob[a:b+1]]
    assert not bad, bad[:5]
    assert work_loads[0] == 0
    print(f"H1 ok: {len(cases)} ranges byte-exact, work_loads balanced")

    # ---------- H2: parse_range
    pr = sr.parse_range
    assert pr(None, 100) is None and pr("garbage", 100) is None and pr("bytes=-", 100) is None
    assert pr("bytes=0-", 100) == (0, 99) and pr("bytes=10-20", 100) == (10, 20)
    assert pr("bytes=-30", 100) == (70, 99) and pr("bytes=-500", 100) == (0, 99)
    assert pr("bytes=90-5000", 100) == (90, 99) and pr("bytes=0-0", 100) == (0, 0)
    assert pr("bytes=0-4,10-20", 100) == (0, 4)
    for h in ("bytes=100-", "bytes=50-10", "bytes=-0"):
        try: pr(h, 100); raise SystemExit("no 416 for " + h)
        except HTTPException as e: assert e.status_code == 416 and e.headers["Content-Range"] == "bytes */100"
    print("H2 ok: range parsing + 416")

    # ---------- H8 / routes via TestClient with a fake streamer
    fid = types.SimpleNamespace(unique_id="abcdef123", file_size=len(blob), mime_type="video/mp4", file_name='a"b\r\n.mp4', file_type="FileType.DOCUMENT")
    async def gfp(self, id):
        if id == 404: raise sr.FIleNotFound
        return fid
    ByteStreamer.get_file_properties = gfp
    multi_clients[0] = None
    sr.class_cache.clear(); sr.class_cache[None] = bs
    from streamgram.server import app
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/config.yml"); assert r.status_code == 400, r.status_code
    r = tc.get("/wp-admin/setup.php"); assert r.status_code == 400
    r = tc.get("/watch/nonsense"); assert r.status_code == 400
    r = tc.get("/5"); assert r.status_code == 403                       # no hash: rejected, no Telegram call
    r = tc.get("/5?hash=wrong0"); assert r.status_code == 403
    r = tc.get("/404?hash=abcdef"); assert r.status_code == 404
    r = tc.get("/5?hash=abcdef"); assert r.status_code == 200 and r.content == blob and r.headers["content-length"] == str(len(blob))
    assert "content-range" not in r.headers and '"' not in r.headers["content-disposition"].split("filename=")[1].strip('"')
    r = tc.get("/abcdef5"); assert r.status_code == 200                  # <hash><id> form
    r = tc.get("/5/name.mp4?hash=abcdef", headers={"Range": "bytes=1000000-"})
    assert r.status_code == 206 and r.content == blob[1000000:] and r.headers["content-range"] == f"bytes 1000000-{len(blob)-1}/{len(blob)}"
    r = tc.get("/5?hash=abcdef", headers={"Range": "bytes=1000000-1100000"}); assert r.status_code == 206 and r.content == blob[1000000:1100001]
    r = tc.get("/5?hash=abcdef", headers={"Range": "bytes=-500"}); assert r.status_code == 206 and r.content == blob[-500:]
    r = tc.get("/5?hash=abcdef", headers={"Range": "bytes=0-0"}); assert r.status_code == 206 and r.content == blob[:1]
    r = tc.get("/5?hash=abcdef", headers={"Range": "bytes=99999999-"}); assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(blob)}"
    fid.file_size = 0
    r = tc.get("/5?hash=abcdef"); assert r.status_code == 200 and r.content == b""
    fid.file_size = len(blob)
    print("H8/H1/H2 ok: routes (400/403/404/200/206/416, Content-Length, Content-Range only on 206)")

    # ---------- H3/H4: render_page
    evil = '<script>alert(1)</script>.pdf'
    async def gfi(client, chat, id):
        return types.SimpleNamespace(unique_id="abcdef999", file_name=evil, mime_type="application/pdf", file_size=2048)
    render_template.get_file_ids = gfi
    page = await render_template.render_page(7, "abcdef")
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page and "2.0 KiB" in page and 'href="' in page
    render_template_vid = types.SimpleNamespace(unique_id="abcdef999", file_name=evil, mime_type="video/mp4", file_size=5)
    async def gfi2(client, chat, id): return render_template_vid
    render_template.get_file_ids = gfi2
    page = await render_template.render_page(7, "abcdef")
    assert "<script>alert(1)" not in page and "<video" in page
    async def gfi3(client, chat, id): return types.SimpleNamespace(unique_id="abcdef999", file_name=None, mime_type=None, file_size=None)
    render_template.get_file_ids = gfi3
    await render_template.render_page(7, "abcdef")   # None mime/name/size must not crash
    print("H3/H4 ok: names escaped, no self-request, None-safe")

    # ---------- H5: channel gate
    from streamgram.utils import access
    from streamgram.bot.plugins import stream as stm
    db = access.access_db
    users = {}
    async def gu(uid): return users.get(uid)
    async def gids(): return [-1005]
    async def cs(c, chat, uid): return None
    async def hjr(uid): return False
    db.get_user = gu; db.group_ids = gids; db.chat_status = cs; db.has_join_request = hjr
    class Admin:
        def __init__(s, uid, bot=False): s.user = types.SimpleNamespace(id=uid, is_bot=bot)
    class Cl:
        def __init__(s, admins): s.admins = admins
        async def get_chat_members(s, chat, filter=None):
            assert filter == enums.ChatMembersFilter.ADMINISTRATORS
            for a in s.admins: yield a
    access._sponsor_cache.clear()
    assert await access.channel_sponsor(Cl([Admin(900), Admin(901)]), -1) is None            # nobody has access
    users[901] = {"status": "approved"}; access._sponsor_cache.clear()
    assert await access.channel_sponsor(Cl([Admin(900), Admin(999, True), Admin(901)]), -1) == 901
    users[901] = {"status": "banned"}; access._sponsor_cache.clear()
    assert await access.channel_sponsor(Cl([Admin(901)]), -1) is None
    access._sponsor_cache.clear()
    assert await access.channel_sponsor(Cl([Admin(222)]), -1) == 222                         # trusted admin
    class Boom:
        def get_chat_members(s, *a, **k): raise RuntimeError("CHAT_ADMIN_REQUIRED")
    access._sponsor_cache.clear(); assert await access.channel_sponsor(Boom(), -2) is None
    print("H5 ok: channel sponsor gate")

    # ---------- H7: /login
    sent = []
    class M:
        def __init__(s, text, chat=5): s.text = text; s.chat = types.SimpleNamespace(id=chat); s.deleted = False
        async def reply_text(s, t, **k): sent.append(t)
        async def delete(s): s.deleted = True
    stm.MY_PASS = "secret"
    added = []
    class PD:
        async def add_user_pass(s, cid, t): added.append((cid, t))
    stm.pass_db = PD()
    await stm.login_password_handler(None, M("secret"));  assert not added                      # not waiting: ignored
    await stm.login_handler(None, M("/login")); assert 5 in stm.login_waiting
    m = M("secret"); await stm.login_password_handler(None, m); assert added == [(5, stm.pass_token("secret"))] and stm.pass_token("secret") != "secret" and m.deleted and 5 not in stm.login_waiting
    await stm.login_handler(None, M("/login")); await stm.login_password_handler(None, M("nope")); assert "Wrong" in sent[-1]
    await stm.login_handler(None, M("/login")); await stm.login_password_handler(None, M("/cancel")); assert "Cancelled" in sent[-1]
    await stm.login_handler(None, M("/login")); stm.login_waiting[5] = 0; await stm.login_password_handler(None, M("secret")); assert "can't wait" in sent[-1]
    # handler is private-only
    import inspect
    src = inspect.getsource(stm)
    assert 'filters.private & (filters.regex("login🔑") | filters.command("login"))' in src
    print("H7 ok: private-only, no pyromod")

    # ---------- H6: chat_status logs PeerIdInvalid once, visibility helper
    from pyrogram.errors import PeerIdInvalid
    import logging
    class Peer:
        async def get_chat_member(s, chat, uid): raise PeerIdInvalid("x")
    class H(logging.Handler):
        def __init__(s): super().__init__(); s.msgs = []
        def emit(s, r): s.msgs.append(r.getMessage())
    h = H(); logging.getLogger().addHandler(h)
    db2 = access.AccessDB.__new__(access.AccessDB); db2._status_cache = {}; db2._warned = {}
    assert await db2.chat_status(Peer(), -1005, 5) is None and await db2.chat_status(Peer(), -1005, 6) is None
    assert len([m for m in h.msgs if "cannot see access chat" in m]) == 1
    ok, detail = await db2.chat_visibility(Peer(), -1005); assert not ok and detail == "PeerIdInvalid"
    print("H6 ok: loud (once) PeerIdInvalid + visibility check")
def test_high_fixes():
    LOOP.run_until_complete(main())


if __name__ == "__main__":
    test_high_fixes()
    print("ALL HIGH FIXES PASS")
