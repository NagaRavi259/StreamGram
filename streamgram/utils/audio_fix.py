"""Fixes the "video plays, no sound" symptom for files whose audio track is a codec no browser
decodes natively (AC3, E-AC3/Dolby Digital Plus, DTS, TrueHD — all common in MKV rips, all
licensed codecs no browser ships a decoder for, in WebCodecs or otherwise).

The server stays a byte passthrough for every other file. Only when a file's chosen audio track
is one of these codecs does this module step in, and even then it only re-encodes the audio
(cheap: a few hundred kb/s of audio, not the video) while the video stream is copied untouched
(`-c:v copy`) — real transcoding is never applied to video.

ffmpeg/ffprobe read the file through the server's own existing streaming endpoint (loopback),
reusing all of its existing Range/auth handling rather than duplicating it. That self-call carries
a marker header (see INTERNAL_HEADER) so the route handler knows to skip this module entirely for
it — otherwise probing or transcoding a file would recursively probe/transcode itself.
"""
import asyncio
import json
import logging
import shutil
import time
from urllib.parse import quote_plus

from streamgram.vars import Var

logger = logging.getLogger("streamgram.utils.audio_fix")

# Codecs no browser's native <video>/MSE audio pipeline decodes (ffprobe's codec_name spelling).
UNSUPPORTED_AUDIO_CODECS = frozenset({"ac3", "eac3", "dts", "truehd", "mlp"})

# Self-HTTP calls ffprobe/ffmpeg make to read the raw file set this header so the route handler
# can tell them apart from a real viewer's request and skip the audio-fix branch for them.
INTERNAL_HEADER_NAME = "x-internal-call"
INTERNAL_HEADER_LINE = "X-Internal-Call: 1\r\n"

PROBE_TIMEOUT_SECONDS = 60  # a multi-GB file's Cues/index can be slow to reach over Telegram
READ_CHUNK_SIZE = 256 * 1024
_DISCONNECT_POLL_SECONDS = 2.0
STALL_TIMEOUT_SECONDS = 45  # no new bytes from ffmpeg for this long -> give up and log loudly


def _which(name):
    return shutil.which(name)


def tools_available():
    """True only when both ffmpeg and ffprobe are on PATH. Checked fresh each time (cheap,
    a couple of filesystem stats) rather than cached, so installing them doesn't need a restart."""
    available = bool(_which("ffmpeg") and _which("ffprobe"))
    if not available:
        logger.debug("tools_available: ffmpeg=%s ffprobe=%s", _which("ffmpeg"), _which("ffprobe"))
    return available


def self_url(id, secure_hash):
    return f"http://127.0.0.1:{Var.PORT}/{int(id)}?hash={quote_plus(secure_hash)}"


class TranscodeBusy(Exception):
    """Raised by begin_transcode() when MAX_CONCURRENT_AUDIO_FIX is already in use."""


class _ConcurrencyGate:
    """A plain, fully synchronous counter: no `await` anywhere in try_enter()/leave(), so
    releasing a slot can be placed in an unconditional `finally` with no risk of the release
    itself being skipped by task cancellation (an `async`/lock-based version had exactly that
    bug — a cancelled cleanup could leave the slot permanently reserved). asyncio is single-
    threaded and cooperative, so a plain int increment/decrement between `await`s is safe
    without a lock."""

    def __init__(self, limit):
        self._limit = limit
        self._count = 0

    def try_enter(self):
        if self._count >= self._limit:
            return False
        self._count += 1
        return True

    def leave(self):
        self._count = max(0, self._count - 1)


_gate = None


def _get_gate():
    global _gate
    if _gate is None or _gate._limit != Var.MAX_CONCURRENT_AUDIO_FIX:
        _gate = _ConcurrencyGate(Var.MAX_CONCURRENT_AUDIO_FIX)
    return _gate


def begin_transcode():
    """Reserves one of MAX_CONCURRENT_AUDIO_FIX transcode slots. Raises TranscodeBusy if none
    are free. Caller must call end_transcode() exactly once afterwards, success or failure —
    place it in an unconditional `finally`, since it is synchronous and cannot be skipped by
    cancellation."""
    if not _get_gate().try_enter():
        logger.warning("begin_transcode: all %s transcode slot(s) in use, refusing", Var.MAX_CONCURRENT_AUDIO_FIX)
        raise TranscodeBusy
    logger.debug("begin_transcode: slot reserved")


def end_transcode():
    _get_gate().leave()
    logger.debug("end_transcode: slot released")


# Per-process cache of probe results, keyed by Telegram message id. A probe result never changes
# for a given file, and re-running ffprobe on every request would be wasteful; this is simply
# never evicted (process lifetime), matching the scale this bot runs at elsewhere (e.g. class_cache
# in stream_routes.py). Only the auto-detect path (force=None) is cached — forced requests are
# explicit test/comparison links and always take the same deterministic path, no probe involved.
_probe_cache = {}

NO_FIX_PLAN = {"needs_fix": False, "audio_track_index": None}
FORCED_FIX_PLAN = {"needs_fix": True, "audio_track_index": 0}


def clear_probe_cache():
    """Test-only hook; production never needs to evict a cached plan."""
    _probe_cache.clear()


async def get_audio_fix_plan(id, secure_hash, force=None):
    """Returns {"needs_fix": bool, "audio_track_index": int|None}. audio_track_index is the
    ffmpeg per-type stream index (what `-map 0:a:N` expects) of the track that would be selected
    by default (the one flagged default, else the first audio track).

    `force` lets a caller skip auto-detection entirely for an explicit comparison link:
    - force=False: always NO_FIX_PLAN (plain passthrough), no probing at all.
    - force=True: always FORCED_FIX_PLAN (transcode track 0), no probing at all — deterministic
      even if probing itself is what's broken, which is the point of a debug/comparison link.
    - force=None (default): auto-detect via ffprobe, cached per file id.

    Auto-detect fails open: any problem probing (ffprobe missing, times out, bad output, no
    audio streams) results in needs_fix=False so a probing hiccup never blocks ordinary playback.
    """
    if force is False:
        logger.debug("get_audio_fix_plan(%s): force=False, plain passthrough", id)
        return NO_FIX_PLAN
    if force is True:
        logger.info("get_audio_fix_plan(%s): force=True, will transcode audio track 0 to AAC", id)
        return FORCED_FIX_PLAN

    if id in _probe_cache:
        logger.debug("get_audio_fix_plan(%s): cached -> %s", id, _probe_cache[id])
        return _probe_cache[id]
    if not Var.ENABLE_AUDIO_FIX:
        logger.debug("get_audio_fix_plan(%s): ENABLE_AUDIO_FIX is off", id)
        _probe_cache[id] = NO_FIX_PLAN
        return NO_FIX_PLAN
    if not tools_available():
        logger.warning("get_audio_fix_plan(%s): ffmpeg/ffprobe not found on PATH, staying on plain passthrough", id)
        _probe_cache[id] = NO_FIX_PLAN
        return NO_FIX_PLAN

    url = self_url(id, secure_hash)
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
        "-headers", INTERNAL_HEADER_LINE, url,
    ]
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_SECONDS)
    except Exception:
        logger.warning(
            "get_audio_fix_plan(%s): ffprobe failed to run after %.1fs", id, time.monotonic() - started, exc_info=True
        )
        _probe_cache[id] = NO_FIX_PLAN
        return NO_FIX_PLAN
    logger.debug("get_audio_fix_plan(%s): ffprobe finished in %.1fs", id, time.monotonic() - started)

    if proc.returncode != 0:
        logger.warning("get_audio_fix_plan(%s): ffprobe exited %s: %s", id, proc.returncode, (err or b"")[-2000:])
        _probe_cache[id] = NO_FIX_PLAN
        return NO_FIX_PLAN

    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError:
        logger.warning("get_audio_fix_plan(%s): ffprobe returned invalid JSON", id)
        _probe_cache[id] = NO_FIX_PLAN
        return NO_FIX_PLAN

    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    logger.debug(
        "get_audio_fix_plan(%s): %d audio stream(s): %s", id, len(audio_streams),
        [(s.get("codec_name"), s.get("disposition", {}).get("default")) for s in audio_streams],
    )
    if not audio_streams:
        logger.debug("get_audio_fix_plan(%s): no audio streams", id)
        _probe_cache[id] = NO_FIX_PLAN
        return NO_FIX_PLAN

    chosen_pos, chosen = next(
        ((i, s) for i, s in enumerate(audio_streams) if s.get("disposition", {}).get("default")),
        (0, audio_streams[0]),
    )
    codec_name = (chosen.get("codec_name") or "").lower()
    if codec_name in UNSUPPORTED_AUDIO_CODECS:
        plan = {"needs_fix": True, "audio_track_index": chosen_pos}
        logger.info(
            "get_audio_fix_plan(%s): audio codec %r unsupported in-browser, will transcode track %s to AAC",
            id, codec_name, chosen_pos,
        )
    else:
        plan = NO_FIX_PLAN
        logger.debug("get_audio_fix_plan(%s): audio codec %r is fine as-is", id, codec_name)
    _probe_cache[id] = plan
    return plan


async def start_ffmpeg_transcode(id, secure_hash, audio_track_index):
    """Spawns ffmpeg: video stream-copied untouched, chosen audio track re-encoded to stereo AAC,
    muxed into a streamable fragmented MP4 written to stdout. Raises if the process can't start."""
    url = self_url(id, secure_hash)
    cmd = [
        "ffmpeg", "-v", "error",
        "-headers", INTERNAL_HEADER_LINE, "-i", url,
        "-map", "0:v:0", "-map", f"0:a:{audio_track_index}",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    logger.info("start_ffmpeg_transcode(%s): %s", id, " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    logger.debug("start_ffmpeg_transcode(%s): pid=%s", id, proc.pid)
    return proc


async def _drain_stderr(proc, id):
    """Logs ffmpeg's stderr line by line as it's produced, not just after the process exits —
    otherwise a stall or a slow failure is invisible until (if ever) the process finally dies."""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            logger.warning("ffmpeg(%s) stderr: %s", id, line.decode(errors="replace").rstrip())
    except Exception:
        logger.debug("_drain_stderr(%s): stopped reading stderr", id, exc_info=True)


async def stream_ffmpeg_output(proc, request, id):
    """Yields ffmpeg's stdout as it's produced, stopping early if the client disconnects or if
    ffmpeg stops producing output for STALL_TIMEOUT_SECONDS. Always releases the transcode slot
    (end_transcode()) as the very first thing in cleanup — synchronous, so it cannot be skipped
    by cancellation — then best-effort kills/reaps the ffmpeg process."""
    stderr_task = asyncio.ensure_future(_drain_stderr(proc, id))
    bytes_sent = 0
    chunks_sent = 0
    started = time.monotonic()
    last_data_at = started
    try:
        while True:
            if await request.is_disconnected():
                logger.info(
                    "stream_ffmpeg_output(%s): client disconnected after %d bytes, %.1fs",
                    id, bytes_sent, time.monotonic() - started,
                )
                break
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(READ_CHUNK_SIZE), timeout=_DISCONNECT_POLL_SECONDS)
            except asyncio.TimeoutError:
                idle_for = time.monotonic() - last_data_at
                if idle_for > STALL_TIMEOUT_SECONDS:
                    logger.warning(
                        "stream_ffmpeg_output(%s): no output from ffmpeg for %.0fs (sent %d bytes so far), "
                        "giving up and ending the response", id, idle_for, bytes_sent,
                    )
                    break
                logger.debug(
                    "stream_ffmpeg_output(%s): waiting for ffmpeg (%d bytes so far, idle %.0fs)",
                    id, bytes_sent, idle_for,
                )
                continue
            if not chunk:
                logger.info(
                    "stream_ffmpeg_output(%s): ffmpeg stdout closed (EOF) after %d bytes, %.1fs",
                    id, bytes_sent, time.monotonic() - started,
                )
                break
            bytes_sent += len(chunk)
            chunks_sent += 1
            last_data_at = time.monotonic()
            if chunks_sent % 40 == 0:
                logger.debug(
                    "stream_ffmpeg_output(%s): %d bytes sent, %.1fs elapsed", id, bytes_sent, last_data_at - started
                )
            yield chunk
    finally:
        end_transcode()
        logger.info(
            "stream_ffmpeg_output(%s): finished, %d bytes / %d chunks over %.1fs",
            id, bytes_sent, chunks_sent, time.monotonic() - started,
        )
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            logger.warning("stream_ffmpeg_output(%s): ffmpeg did not exit within 5s of being killed", id)
        stderr_task.cancel()
        if proc.returncode not in (0, None, -9):
            logger.warning("stream_ffmpeg_output(%s): ffmpeg exited with code %s", id, proc.returncode)
