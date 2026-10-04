"""Offline tests for link expiry, the logging setup and the low-severity fixes.

Run:  python tests/test_expiry_logging_low.py   (or: pytest tests)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
import json
import logging
import subprocess
import tempfile
import time
import types

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _shared import LOOP, Recorder, install_fakes, quiet_database, user  # noqa: E402

os.environ.update(API_ID="1", API_HASH="x", BOT_TOKEN="1:x", BIN_CHANNEL="-100", OWNER_ID="111", TRUSTED_USERS="222",
                  USER_GROUP_ID="-1005", MONGO_SCHEMA="mongodb", MONGO_HOST="127.0.0.1", MONGO_USERNAME="u",
                  MONGO_PASSWORD="p", FQDN="example.com")
os.environ.pop("DYNO", None)

UID = "AgADabcdefGHIJKLMNOP"


def read(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def var_values(env_overrides, remove=()):
    """Evaluate streamgram.vars in a clean process (no config.env in the working directory)."""
    env = {k: v for k, v in os.environ.items() if k not in remove}
    env.update(env_overrides, PYTHONPATH=ROOT)
    code = ("import json; from streamgram.vars import Var; "
            "print(json.dumps({k: getattr(Var, k) for k in ('URL','HAS_SSL','NO_PORT','UPDATES_CHANNEL','MY_PASS',"
            "'LOG_LEVEL','LOG_LIBS_LEVEL','LOG_DIR','LOG_MAX_MB','LOG_BACKUPS','WORKERS')}))")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=tempfile.mkdtemp(), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout)


async def main():
    quiet_database()
    import streamgram.server  # same import order as the application
    from streamgram.utils.access import access_db as db
    from streamgram.utils.link_expiry import link_expiry as le, parse_duration, format_ttl, PRESETS
    install_fakes(db)

    # ---------------------------------------------------------------- link expiry: durations
    assert parse_duration("90m") == 5400 and parse_duration("12h") == 43200 and parse_duration("3d") == 259200
    assert parse_duration("2w") == 1209600 and parse_duration("1d12h") == 129600 and parse_duration(" 1 d 2 h ") == 93600
    for bad in ("", "abc", "10", "0m", "-5m", "5x", "1d junk", "99999999d"):
        try:
            parse_duration(bad); raise SystemExit(f"accepted {bad!r}")
        except ValueError:
            pass
    assert format_ttl(None) == "unlimited" and format_ttl(86400) == "1d" and format_ttl(5400) == "1h 30m"
    assert [s for _, s in PRESETS] == sorted(s for _, s in PRESETS)
    print("expiry ok: duration parsing")

    # a database failure must not break links: last known answer, else unlimited
    async def broken(*a, **k): raise RuntimeError("db down")
    healthy = db.links.find_one
    await db.links.insert_one({"msg_id": 31, "expires_at": time.time() + 600}); le._cache.clear()
    assert not await le.is_expired(31)
    db.links.find_one = broken
    le._cache[31] = (time.time() - 3600, time.time() - 5)           # stale entry that says "expired"
    assert await le.is_expired(31)                                   # keeps the last known answer
    le._cache.clear(); assert not await le.is_expired(32)            # nothing known -> not blocked
    db.links.find_one = healthy; le._cache.clear()
    db.links.docs.clear()
    print("expiry ok: database failure degrades gracefully")

    # ---------------------------------------------------------------- link expiry: settings and registration
    assert await le.register(10, 5) is None and not db.links.docs               # default is unlimited, nothing stored
    await le.set_default(3600)
    exp = await le.register(11, 5)
    assert abs(exp - (time.time() + 3600)) < 5 and db.links.docs[0]["msg_id"] == 11 and db.links.docs[0]["uid"] == 5
    assert not await le.is_expired(11) and not await le.is_expired(999)         # unknown (older) links never expire
    await le.set_personal(6, None)                                               # personal "unlimited" beats a finite default
    assert await le.register(12, 6) is None and await le.effective(6) == (None, "personal")
    await le.set_personal(7, 60)
    assert abs(await le.register(13, 7) - (time.time() + 60)) < 5 and await le.effective(7) == (60, "personal")
    await le.clear_personal(7)
    assert await le.effective(7) == (3600, "default")
    await le.set_default(None)
    assert await le.effective(7) == (None, "default") and await le.register(14, 7) is None
    db.links.docs[0]["expires_at"] = int(time.time()) - 1; le._cache.clear()
    assert await le.is_expired(11)
    print("expiry ok: default / personal / unlimited / expired")

    # ---------------------------------------------------------------- link expiry: enforced by the web server
    from fastapi.testclient import TestClient
    from pyrogram import raw
    from streamgram.server import app, stream_routes as sr
    from streamgram.utils.custom_dl import ByteStreamer
    from streamgram.utils import render_template as rt
    from streamgram.bot import multi_clients, work_loads
    blob = b"y" * 4000
    class Sess:
        async def send(self, req):
            return raw.types.upload.File(type=None, mtime=0, bytes=blob[req.offset:req.offset + req.limit])
    bs = object.__new__(ByteStreamer); bs.client = None
    async def gms(cl, f): return Sess()
    async def gl(f): return None
    bs.generate_media_session = gms; ByteStreamer.get_location = staticmethod(gl)
    fid = types.SimpleNamespace(unique_id=UID, file_size=len(blob), mime_type="video/mp4", file_name="v.mp4", file_type="FileType.DOCUMENT")
    async def gfp(self, mid): return fid
    ByteStreamer.get_file_properties = gfp
    async def gfi(client, chat, mid): return types.SimpleNamespace(unique_id=UID, file_name="v.mp4", mime_type="video/mp4", file_size=5)
    rt.get_file_ids = gfi
    multi_clients[0] = None; work_loads[0] = 0; sr.class_cache.clear(); sr.class_cache[None] = bs
    tc = TestClient(app, raise_server_exceptions=False)
    good = f"hash={UID[:12]}"
    assert tc.get(f"/5?{good}").status_code == 200                              # no record: unlimited
    await db.links.insert_one({"msg_id": 5, "uid": 1, "created_at": 1, "expires_at": int(time.time()) + 600}); le._cache.clear()
    assert tc.get(f"/5?{good}").status_code == 200 and tc.get(f"/watch/5/?{good}").status_code == 200
    db.links.docs[-1]["expires_at"] = int(time.time()) - 1; le._cache.clear()
    r = tc.get(f"/5?{good}"); assert r.status_code == 410 and "expired" in r.text.lower()
    assert tc.get(f"/watch/5/?{good}").status_code == 410
    assert tc.get(f"/{UID[:6]}5").status_code == 410                             # legacy link form too
    assert tc.get("/5?hash=AgADab9").status_code == 403                          # expiry is not revealed without a valid hash
    print("expiry ok: 410 for expired links (file + watch page), hash checked first")

    # ---------------------------------------------------------------- link expiry: deep link and link text
    from streamgram.bot.plugins import start_help as sh, stream as stm, access_admin as aa, extra, admin as adminmod
    async def _true(uid): return True
    sh.db = types.SimpleNamespace(is_user_exist=_true); sh.Var.UPDATES_CHANNEL = None
    media = types.SimpleNamespace(file_unique_id=UID, file_name="a.zip", file_size=2048)
    async def get_messages(chat, mid): return types.SimpleNamespace(empty=False, document=media)
    b = Recorder(); b.get_messages = get_messages
    m = Recorder(from_user=user(5), chat=types.SimpleNamespace(id=5), text=f"/start 5_{UID[:12]}")
    await sh.start(b, m); assert "not valid" in m.sent[0][0]                    # message 5 is expired above
    m = Recorder(from_user=user(5), chat=types.SimpleNamespace(id=5), text=f"/start 6_{UID[:12]}")
    await sh.start(b, m); assert "/6/?hash=" in m.sent[0][0]

    stm.db = types.SimpleNamespace(is_user_exist=_true); stm.MY_PASS = None
    doc = types.SimpleNamespace(file_unique_id=UID, file_name="f.zip", file_size=2048)
    async def send(seconds):
        await le.set_default(seconds)
        log_msg = Recorder(id=77, document=doc)
        msg = Recorder(from_user=user(5), chat=types.SimpleNamespace(id=5), document=doc)
        async def forward(chat_id): return log_msg
        msg.forward = forward
        await stm.private_receive_handler(Recorder(), msg)
        return msg.sent[-1][0]
    text = await send(None); assert "expires in" not in text and "♻️" in text
    db.links.docs[:] = [d for d in db.links.docs if d["msg_id"] != 77]
    text = await send(7 * 86400); assert "This link expires in 7d" in text and "♻️" not in text
    assert any(d["msg_id"] == 77 and d["uid"] == 5 for d in db.links.docs)
    await le.set_default(None)
    print("expiry ok: deep link honours expiry, link message shows the lifetime")

    # ---------------------------------------------------------------- link expiry: admin menu
    aa.StreamBot.username = "testbot"
    owner = user(111, "Owner")
    cq = Recorder(data="adm:x:0", from_user=owner, message=Recorder(text="menu"))
    await aa.cb_expiry(Recorder(), cq)
    labels = [btn.text for row in cq.message.edits[-1][1]["reply_markup"].inline_keyboard for btn in row]
    assert "7 days" in labels and "♾ Unlimited" in labels and "✏️ Custom" in labels and "↩ Use default" not in labels
    cq = Recorder(data="adm:xp:0:86400", from_user=owner, message=Recorder(text="menu"))
    await aa.cb_expiry_set(Recorder(), cq); assert await le.get_default() == 86400 and "1d" in cq.message.edits[-1][0]
    await db.users.insert_one({"id": 55, "status": "approved", "first_name": "P", "updated_at": 1})
    cq = Recorder(data="adm:xp:55:3600", from_user=owner, message=Recorder(text="menu"))
    await aa.cb_expiry_set(Recorder(), cq); assert await le.effective(55) == (3600, "personal")
    page = cq.message.edits[-1][0]; assert "Link expiry: <b>1h</b> (personal)" in page
    cq = Recorder(data="adm:x:55", from_user=owner, message=Recorder(text="menu"))
    await aa.cb_expiry(Recorder(), cq)
    assert "↩ Use default" in [btn.text for row in cq.message.edits[-1][1]["reply_markup"].inline_keyboard for btn in row]
    cq = Recorder(data="adm:xp:55:unlimited", from_user=owner, message=Recorder(text="menu"))
    await aa.cb_expiry_set(Recorder(), cq); assert await le.effective(55) == (None, "personal")
    cq = Recorder(data="adm:xp:55:default", from_user=owner, message=Recorder(text="menu"))
    await aa.cb_expiry_set(Recorder(), cq); assert await le.effective(55) == (86400, "default")
    cq = Recorder(data="adm:xp:55:custom", from_user=owner, message=Recorder(text="menu"))
    await aa.cb_expiry_set(Recorder(), cq); assert aa.inputs[111] == {"kind": "ttl", "uid": 55}
    m = Recorder(from_user=owner, text="nonsense"); await aa.owner_input(Recorder(), m)
    assert "e.g. 30m" in m.sent[0][0] and 111 in aa.inputs                       # bad input keeps waiting
    m = Recorder(from_user=owner, text="2d12h"); await aa.owner_input(Recorder(), m)
    assert await le.effective(55) == (216000, "personal") and 111 not in aa.inputs
    aa.inputs[111] = {"kind": "ttl", "uid": 0}
    m = Recorder(from_user=owner, text="12h"); await aa.owner_input(Recorder(), m)
    assert await le.get_default() == 43200
    await le.set_default(None)
    assert any(h["action"] == "expiry_set" for h in db.history.docs)
    home, _ = await aa.home_view(); assert "Default link expiry: <b>unlimited</b>" in home
    print("expiry ok: admin menu (default, personal, custom, unlimited, reset)")

    # ---------------------------------------------------------------- logging
    from streamgram.utils.logging_config import setup_logging, to_level, LIBRARIES
    assert to_level("debug") == logging.DEBUG and to_level("WARNING") == logging.WARNING and to_level("nonsense") == logging.INFO
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, {n: logging.getLogger(n).level for n in LIBRARIES})
    def flush():
        for h in root.handlers: h.flush()
    try:
        d = tempfile.mkdtemp()
        setup_logging("INFO", d, 100000, 2, "WARNING")
        lg = logging.getLogger("streamgram.test")
        lg.debug("only-debug"); lg.info("only-info"); lg.warning("only-warning"); lg.error("only-error")
        logging.getLogger("pyrogram").info("library-chatter"); logging.getLogger("pyrogram").warning("library-warning")
        flush()
        info, err = read(f"{d}/info.log"), read(f"{d}/error.log")
        assert "only-info" in info and "only-debug" not in info and "only-warning" not in info and "library-chatter" not in info
        assert "only-warning" in err and "only-error" in err and "only-info" not in err and "library-warning" in err
        d2 = tempfile.mkdtemp()
        setup_logging("DEBUG", d2, 100000, 2, "WARNING")
        logging.getLogger("streamgram.test").debug("now-debug"); logging.getLogger("pyrogram").debug("lib-debug"); flush()
        assert "now-debug" in read(f"{d2}/info.log") and "lib-debug" not in read(f"{d2}/info.log")
        d3 = tempfile.mkdtemp()
        setup_logging("ERROR", d3, 100000, 2, "WARNING")
        logging.getLogger("streamgram.test").warning("hidden"); logging.getLogger("streamgram.test").error("shown"); flush()
        assert "hidden" not in read(f"{d3}/error.log") and "shown" in read(f"{d3}/error.log")
        # bounded: tiny files rotate, only `backups` copies are kept
        d4 = tempfile.mkdtemp()
        setup_logging("INFO", d4, 2000, 2, "WARNING")
        for i in range(2000): logging.getLogger("streamgram.test").info("x" * 80 + str(i))
        flush()
        files = sorted(f for f in os.listdir(d4) if f.startswith("info")); size = sum(os.path.getsize(os.path.join(d4, f)) for f in files)
        assert files == ["info.log", "info.log.1", "info.log.2"] and size < 3 * 2000 + 400, (files, size)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h); h.close()
        for h in saved[0]: root.addHandler(h)
        root.setLevel(saved[1])
        for n, lv in saved[2].items(): logging.getLogger(n).setLevel(lv)
    # no module logs through the root logger or print()s any more (banners in __main__ excepted)
    import glob, re
    for path in glob.glob(os.path.join(ROOT, "streamgram", "**", "*.py"), recursive=True):
        src = read(path)
        if path.endswith("logging_config.py"): continue
        assert not re.search(r"\blogging\.(info|debug|warning|error|critical|exception)\(", src), path
        if not path.endswith("__main__.py"): assert not re.search(r"^\s*print\(", src, re.M), path
    print("logging ok: info/error split, levels, libraries, rotation bound, no stray prints")

    # ---------------------------------------------------------------- configuration parsing (L2, L3)
    base = var_values({}, remove=("HAS_SSL", "NO_PORT", "UPDATES_CHANNEL", "MY_PASS", "LOG_LEVEL", "LOG_DIR", "LOG_MAX_MB", "LOG_BACKUPS", "LOG_LIBS_LEVEL"))
    assert base["URL"].startswith("http://") and base["HAS_SSL"] is False and base["NO_PORT"] is False
    assert base["UPDATES_CHANNEL"] is None and base["MY_PASS"] is None
    assert (base["LOG_LEVEL"], base["LOG_LIBS_LEVEL"], base["LOG_DIR"], base["LOG_MAX_MB"], base["LOG_BACKUPS"]) == ("INFO", "WARNING", "logs", 5, 3)
    assert base["WORKERS"] == 3
    for text in ("false", "0", "", "no", "False"): assert var_values({"HAS_SSL": text, "NO_PORT": text})["HAS_SSL"] is False
    on = var_values({"HAS_SSL": "True", "NO_PORT": "1"}); assert on["URL"].startswith("https://") and on["NO_PORT"] is True
    for text in ("None", "none", "", "  "): assert var_values({"UPDATES_CHANNEL": text})["UPDATES_CHANNEL"] is None
    assert var_values({"UPDATES_CHANNEL": " mychannel "})["UPDATES_CHANNEL"] == "mychannel"
    assert var_values({"LOG_LEVEL": "debug", "LOG_MAX_MB": "9"})["LOG_MAX_MB"] == 9
    print("config ok: booleans, UPDATES_CHANNEL, MY_PASS, log settings")

    # ---------------------------------------------------------------- remaining low items
    # L4 one shared Database per name and one Mongo client
    from streamgram.utils.database import Database, get_client
    a = Database.shared("zz_test_a")
    assert a is Database.shared("zz_test_a") and a is not Database.shared("zz_test_b") and a._client is get_client()
    assert db.users.__class__.__name__ == "FakeCol"  # (collections are swapped by the fakes; the client is shared in production)
    # L5 login token
    assert stm.pass_token("secret") == stm.pass_token("secret") and stm.pass_token("secret") not in ("secret", "") and len(stm.pass_token("x")) == 64
    # L6 /stats is owner-only and also answers to /status
    src = read(os.path.join(ROOT, "streamgram/bot/plugins/extra.py"))
    assert "filters.command(['stats', 'status'])" in src and "filters.user(list(Var.OWNER_ID))" in src
    upd = Recorder(); await extra.stats(None, upd); assert "Bot Uptime" in upd.sent[0][0]
    from streamgram.utils.formatting import readable_time, get_readable_file_size
    assert readable_time(3725) == "1h2m5s" and get_readable_file_size(2048) == "2.0KB" and get_readable_file_size(None) == "0B"
    assert not os.path.exists(os.path.join(ROOT, "utils_bot.py")) and not os.path.exists(os.path.join(ROOT, "streamgram/utils/file_size.py"))
    # L8 requirements
    req = read(os.path.join(ROOT, "requirements.txt")).lower().split()
    assert "fastapi" in req and "uvicorn" in req and "pyromod" not in req
    proc = json.loads(read(os.path.join(ROOT, "process.json")))["apps"][0]
    assert proc["script"] == "start_bot.py" and "\\" not in json.dumps(proc)
    # L9 stale menu and system rows in the history
    cq = Recorder(message=None); await aa.edit(cq, "x", []); assert cq.answers and cq.answers[0][1]
    assert "(system)" in aa.history_text([{"ts": 1, "uid": 0, "action": "group_added", "by": 111, "name": ""}], 1, "H")
    # L11 template placeholder
    assert "__MEDIA__" in read(os.path.join(ROOT, "streamgram/template/req.html")) and "<tag" not in read(os.path.join(ROOT, "streamgram/template/req.html"))
    async def gvid(client, chat, mid): return types.SimpleNamespace(unique_id=UID, file_name="v.mp4", mime_type="audio/mpeg", file_size=5)
    rt.get_file_ids = gvid
    page = await rt.render_page(8, UID[:12]); assert "<audio" in page and "__MEDIA__" not in page
    # request log: still written, to the same CSV, without blocking
    import csv
    log = os.path.join(tempfile.mkdtemp(), "requests.csv"); real = streamgram.server.log_file_path
    streamgram.server.log_file_path = log
    try:
        tc.get("/probe-path"); tc.get("/other")
    finally:
        streamgram.server.log_file_path = real
    rows = list(csv.reader(open(log)))
    assert len(rows) == 2 and rows[0][2].endswith("/probe-path") and len(rows[0]) == 3
    print("low ok: shared DB, login token, /stats owner-only, formatting module, requirements, stale menus, template, request CSV")


def test_expiry_logging_low():
    LOOP.run_until_complete(main())


if __name__ == "__main__":
    test_expiry_logging_low()
    print("ALL EXPIRY / LOGGING / LOW FIXES PASS")
