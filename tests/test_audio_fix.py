"""Regression tests for the AC3/E-AC3/DTS/TrueHD audio fix (streamgram/utils/audio_fix.py).

Run from anywhere:  python tests/test_audio_fix.py
Covers both fast offline unit checks (mocked ffprobe/ffmpeg, no real process spawned) and a real
end-to-end check: a genuine uvicorn server, a genuine synthetic E-AC3-in-MKV file built with the
real ffmpeg, and the real ffprobe/ffmpeg binaries doing the actual probe-then-transcode over a
real loopback HTTP connection — the same path production traffic takes. The output is itself
decoded with real ffmpeg/ffprobe to confirm it is a correct, playable file (video untouched,
audio now AAC), not just that no exception was raised.

No real Telegram/MongoDB access: the ByteStreamer is monkeypatched to serve a local file's bytes
(same technique as tests/test_high_fixes.py's H1), and the database layer is the in-memory fakes
from tests/_shared.py.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

# A real free port, chosen before Var is ever imported: audio_fix.self_url() reads Var.PORT, so
# the loopback self-calls ffprobe/ffmpeg make must land on whatever port the real server binds.
_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_sock.bind(("127.0.0.1", 0))
FREE_PORT = _sock.getsockname()[1]
_sock.close()

import tempfile as _tempfile
os.environ["LOG_DIR"] = _tempfile.mkdtemp(prefix="ftl-test-logs-")
os.environ.update(
    API_ID="1", API_HASH="x", BOT_TOKEN="1:x", BIN_CHANNEL="-100", OWNER_ID="111", TRUSTED_USERS="222",
    USER_GROUP_ID="-1005", MONGO_SCHEMA="mongodb", MONGO_HOST="127.0.0.1", MONGO_USERNAME="u", MONGO_PASSWORD="p",
    FQDN="127.0.0.1", PORT=str(FREE_PORT), MAX_CONCURRENT_AUDIO_FIX="1",
)
os.environ.pop("DYNO", None)

import asyncio
import json
import types
from _shared import LOOP  # noqa: E402


async def main():
    from _shared import quiet_database, install_fakes
    quiet_database()
    import streamgram.server
    from streamgram.utils.access import access_db as _access_db
    install_fakes(_access_db)
    from streamgram.server import stream_routes as sr
    from streamgram.utils import audio_fix, render_template
    from streamgram.utils.custom_dl import ByteStreamer
    from streamgram.bot import work_loads, multi_clients
    from streamgram.vars import Var

    # ---------- unit: codec list sanity
    for bad in ("ac3", "eac3", "dts", "truehd", "mlp"):
        assert bad in audio_fix.UNSUPPORTED_AUDIO_CODECS, bad
    for fine in ("aac", "opus", "mp3", "vorbis", "flac", "pcm_s16le"):
        assert fine not in audio_fix.UNSUPPORTED_AUDIO_CODECS, fine
    print("U1 ok: unsupported-codec list contains the Dolby/DTS set, not ordinary codecs")

    # ---------- unit: concurrency gate (fully synchronous: see the module docstring on why —
    # a cancelled cleanup must never be able to skip releasing a slot)
    audio_fix._gate = None  # force a fresh gate at the test's MAX_CONCURRENT_AUDIO_FIX=1
    audio_fix.begin_transcode()
    try:
        audio_fix.begin_transcode()
        raise SystemExit("second begin_transcode should have raised TranscodeBusy")
    except audio_fix.TranscodeBusy:
        pass
    audio_fix.end_transcode()
    audio_fix.begin_transcode()  # slot freed, should succeed again
    audio_fix.end_transcode()
    print("U2 ok: concurrency gate enforces MAX_CONCURRENT_AUDIO_FIX and frees slots")

    # ---------- unit: force=True/False bypass probing entirely and skip the cache
    real_exec = asyncio.create_subprocess_exec  # captured before any monkeypatching below
    real_tools_available = audio_fix.tools_available
    audio_fix.clear_probe_cache()
    asyncio.create_subprocess_exec = None  # force=True/False must never spawn ffprobe
    assert await audio_fix.get_audio_fix_plan(201, "h", force=False) == audio_fix.NO_FIX_PLAN
    assert await audio_fix.get_audio_fix_plan(201, "h", force=True) == audio_fix.FORCED_FIX_PLAN
    assert 201 not in audio_fix._probe_cache  # forced results are never cached
    print("U2b ok: force=True/False short-circuit probing deterministically, uncached")

    # ---------- unit: get_audio_fix_plan, mocked ffprobe (no real process)
    audio_fix.clear_probe_cache()

    def fake_streams(streams):
        async def fake_exec(*cmd, stdout=None, stderr=None):
            class P:
                returncode = 0
                async def communicate(self):
                    return json.dumps({"streams": streams}).encode(), b""
            return P()
        return fake_exec

    audio_fix.tools_available = lambda: True

    # default-flagged E-AC3 track should be picked over a non-default AAC track that sorts first
    asyncio.create_subprocess_exec = fake_streams([
        {"codec_type": "audio", "codec_name": "aac", "disposition": {"default": 0}},
        {"codec_type": "audio", "codec_name": "eac3", "disposition": {"default": 1}},
    ])
    plan = await audio_fix.get_audio_fix_plan(101, "hash101")
    assert plan == {"needs_fix": True, "audio_track_index": 1}, plan
    print("U3 ok: picks the disposition-default track, not just the first one")

    # cached: a second call must not re-invoke ffprobe at all
    asyncio.create_subprocess_exec = None  # would raise if called
    plan2 = await audio_fix.get_audio_fix_plan(101, "hash101")
    assert plan2 == plan
    print("U4 ok: probe result is cached per file id")

    # AAC-only file: no fix needed
    audio_fix.clear_probe_cache()
    asyncio.create_subprocess_exec = fake_streams([{"codec_type": "audio", "codec_name": "aac", "disposition": {"default": 1}}])
    plan3 = await audio_fix.get_audio_fix_plan(102, "hash102")
    assert plan3 == audio_fix.NO_FIX_PLAN, plan3
    print("U5 ok: AAC audio is left alone")

    # no audio streams at all: fails open, never crashes
    audio_fix.clear_probe_cache()
    asyncio.create_subprocess_exec = fake_streams([{"codec_type": "video", "codec_name": "h264"}])
    plan4 = await audio_fix.get_audio_fix_plan(103, "hash103")
    assert plan4 == audio_fix.NO_FIX_PLAN, plan4
    print("U6 ok: video-only file (no audio streams) is left alone, no crash")

    # ffprobe missing/disabled: fails open
    audio_fix.clear_probe_cache()
    audio_fix.tools_available = lambda: False
    asyncio.create_subprocess_exec = None  # would raise if called; must not be, tools gate first
    plan5 = await audio_fix.get_audio_fix_plan(104, "hash104")
    assert plan5 == audio_fix.NO_FIX_PLAN, plan5
    print("U7 ok: missing ffmpeg/ffprobe fails open to plain passthrough, never calls ffprobe")

    asyncio.create_subprocess_exec = real_exec
    audio_fix.tools_available = real_tools_available
    audio_fix.clear_probe_cache()

    # ---------- unit: the transcode slot is released even when the stream is cancelled mid-read
    # (the actual bug that caused "worked once, never again" in production: an async-lock-based
    # gate release could itself be skipped by cancellation, permanently wedging every slot).
    class FakeStream:
        async def read(self, n):
            await asyncio.sleep(3600)  # never produces data on its own
        async def readline(self):
            await asyncio.sleep(3600)

    class FakeProc:
        def __init__(self):
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.returncode = None
            self.pid = -1
        def kill(self):
            self.returncode = -9
        async def wait(self):
            return self.returncode

    class FakeRequest:
        async def is_disconnected(self):
            return False

    audio_fix._gate = None
    audio_fix.begin_transcode()
    assert audio_fix._gate._count == 1
    gen = audio_fix.stream_ffmpeg_output(FakeProc(), FakeRequest(), 999)
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.1)  # let it reach the stdout.read() await
    task.cancel()
    try:
        await task
        raise SystemExit("expected the cancelled task to raise CancelledError")
    except asyncio.CancelledError:
        pass
    assert audio_fix._gate._count == 0, "transcode slot leaked after cancellation"
    print("U8 ok: the transcode slot is freed even when the stream is cancelled mid-read")

    if not HAVE_FFMPEG:
        print("SKIP: real ffmpeg/ffprobe not found on PATH, skipping the end-to-end check")
        return

    # ---------- end-to-end: real ffmpeg fixture, real server, real ffprobe/ffmpeg round trip
    FIXTURE_DURATION = 6  # seconds; long enough, with 1s keyframes, to make seeking meaningful
    tmp_dir = tempfile.mkdtemp(prefix="ftl-audiofix-")
    fixture_path = os.path.join(tmp_dir, "fixture.mkv")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=10:duration={FIXTURE_DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={FIXTURE_DURATION}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-keyint_min", "10",
            "-c:a", "eac3", "-b:a", "192k",
            "-f", "matroska", fixture_path,
        ],
        check=True,
    )
    fixture_bytes = open(fixture_path, "rb").read()
    assert len(fixture_bytes) > 1000

    file_id = types.SimpleNamespace(
        unique_id="fixtureuid123", file_size=len(fixture_bytes), mime_type="video/x-matroska",
        file_name="fixture.mkv", file_type="FileType.DOCUMENT",
    )

    async def get_file_properties(self, id):
        return file_id
    ByteStreamer.get_file_properties = get_file_properties

    from pyrogram import raw

    class FakeSession:
        async def send(self, req):
            return raw.types.upload.File(type=None, mtime=0, bytes=fixture_bytes[req.offset:req.offset + req.limit])

    bs = object.__new__(ByteStreamer)
    bs.client = None
    async def gms(c, f):
        return FakeSession()
    async def gl(f):
        return None
    bs.generate_media_session = gms
    ByteStreamer.get_location = staticmethod(gl)

    work_loads[0] = 0
    multi_clients[0] = None
    sr.class_cache.clear()
    sr.class_cache[None] = bs

    import uvicorn
    config = uvicorn.Config(streamgram.server.app, host="127.0.0.1", port=FREE_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn server did not start in time"

    try:
        import requests
        base = f"http://127.0.0.1:{FREE_PORT}"
        msg_id = 55
        secure_hash = "fixtureu"  # a prefix of unique_id, >= MIN_HASH_LENGTH: passes the legacy hash check

        r = requests.get(f"{base}/{msg_id}?hash={secure_hash}", timeout=30)
        assert r.status_code == 200, (r.status_code, r.text[:500])
        assert r.headers.get("content-type") == "video/mp4", r.headers
        out_bytes = r.content
        assert len(out_bytes) > 1000
        print(f"E1 ok: server responded 200 video/mp4, {len(out_bytes)} bytes")

        out_path = os.path.join(tmp_dir, "out.mp4")
        with open(out_path, "wb") as f:
            f.write(out_bytes)

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", out_path],
            capture_output=True, check=True,
        )
        streams = json.loads(probe.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next(s for s in streams if s["codec_type"] == "audio")
        assert video["codec_name"] == "h264", video  # video stream-copied untouched
        assert audio["codec_name"] == "aac", audio   # audio re-encoded from eac3
        print("E2 ok: output video is still h264 (copied), audio is now AAC (was E-AC3)")

        decode = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", out_path, "-f", "null", "-"],
            capture_output=True,
        )
        assert decode.returncode == 0, decode.stderr
        assert decode.stderr == b"", decode.stderr
        print("E3 ok: output decodes cleanly end to end (zero warnings)")

        # a second request for the same id must reuse the cached probe (no second ffprobe spawn
        # observable from here, but it must still succeed and still be fixed)
        r2 = requests.get(f"{base}/{msg_id}?hash={secure_hash}", timeout=30)
        assert r2.status_code == 200 and r2.headers.get("content-type") == "video/mp4"
        print("E4 ok: repeat request for the same file is served fixed again (cached plan)")

        # the internal-call header must make the server skip the fix branch entirely and fall
        # through to plain byte passthrough — this is exactly what ffprobe/ffmpeg's own self-call
        # relies on to avoid recursing into itself.
        r3 = requests.get(
            f"{base}/{msg_id}?hash={secure_hash}", timeout=30,
            headers={"X-Internal-Call": "1"},
        )
        assert r3.status_code == 200
        assert r3.headers.get("content-type") == "video/x-matroska", r3.headers
        assert r3.content == fixture_bytes
        print("E5 ok: the internal-call marker header bypasses the fix branch (no recursion)")

        # ?audiofix=0 must force the plain original through, even though auto-detect would fix it
        r4 = requests.get(f"{base}/{msg_id}?hash={secure_hash}&audiofix=0", timeout=30)
        assert r4.status_code == 200
        assert r4.headers.get("content-type") == "video/x-matroska", r4.headers
        assert r4.content == fixture_bytes
        print("E6 ok: ?audiofix=0 forces the original (unfixed) stream, for comparison")

        # ?audiofix=1 must force the fixed version through deterministically
        r5 = requests.get(f"{base}/{msg_id}?hash={secure_hash}&audiofix=1", timeout=30)
        assert r5.status_code == 200
        assert r5.headers.get("content-type") == "video/mp4", r5.headers
        assert len(r5.content) > 1000
        print("E7 ok: ?audiofix=1 forces the fixed stream, for comparison")

        # ---------- duration probing (what makes the seek-capable player's bar correct)
        duration = await audio_fix.probe_duration_seconds(msg_id, secure_hash)
        assert duration is not None and abs(duration - FIXTURE_DURATION) < 0.5, duration
        print(f"E8 ok: probe_duration_seconds reports ~{duration:.1f}s for a {FIXTURE_DURATION}s fixture")

        # ---------- ?start=N actually seeks: output should cover roughly [N, duration], not the
        # whole file again (the actual feature being added here, no disk cache: a seek just
        # starts a fresh transcode from the requested point)
        seek_to = 3
        r6 = requests.get(f"{base}/{msg_id}?hash={secure_hash}&audiofix=1&start={seek_to}", timeout=30)
        assert r6.status_code == 200 and r6.headers.get("content-type") == "video/mp4"
        seek_out_path = os.path.join(tmp_dir, "seek_out.mp4")
        with open(seek_out_path, "wb") as f:
            f.write(r6.content)
        seek_probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", seek_out_path],
            capture_output=True, check=True,
        )
        seek_duration = float(json.loads(seek_probe.stdout)["format"]["duration"])
        expected_remaining = FIXTURE_DURATION - seek_to
        assert abs(seek_duration - expected_remaining) < 1.5, (seek_duration, expected_remaining)
        decode2 = subprocess.run(["ffmpeg", "-v", "error", "-i", seek_out_path, "-f", "null", "-"], capture_output=True)
        assert decode2.returncode == 0 and decode2.stderr == b"", decode2.stderr
        print(f"E9 ok: ?start={seek_to} yields ~{seek_duration:.1f}s of output (expected ~{expected_remaining}s), decodes cleanly")

        # ---------- the watch page uses the seek-capable custom player when the fix applies,
        # and the plain Plyr-based page when it's forced off
        async def get_file_ids(client, chat, mid):
            return file_id
        render_template.get_file_ids = get_file_ids

        watch_fixed = requests.get(f"{base}/watch/{msg_id}/?hash={secure_hash}&audiofix=1", timeout=30)
        assert watch_fixed.status_code == 200
        body = watch_fixed.text
        assert "audio-fix player" not in body  # sanity: not literally searching for our own comment
        assert "var totalDuration" in body and "var baseStreamUrl" in body
        assert f"start=' + encodeURIComponent" in body  # the seek-reload logic is present
        assert f"&amp;audiofix=1" not in body  # the embedded stream URL must be the raw (unescaped) one
        assert "&audiofix=1" in body
        print("E10 ok: watch page renders the seek-capable custom player when the fix applies")

        watch_plain = requests.get(f"{base}/watch/{msg_id}/?hash={secure_hash}&audiofix=0", timeout=30)
        assert watch_plain.status_code == 200
        assert "var totalDuration" not in watch_plain.text
        assert "plyr" in watch_plain.text.lower()
        print("E11 ok: watch page falls back to the plain player when ?audiofix=0 forces the fix off")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_audio_fix():
    LOOP.run_until_complete(main())


if __name__ == "__main__":
    test_audio_fix()
    print("ALL AUDIO-FIX TESTS PASS")
