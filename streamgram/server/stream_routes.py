# Taken from megadlbot_oss <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/webserver/routes.py>
# Thanks to Eyaadh <https://github.com/eyaadh>

import re
import time
import math
import traceback
import logging
logger = logging.getLogger("streamgram.server.stream_routes")
import secrets
import mimetypes
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from streamgram.bot import multi_clients, work_loads, StreamBot
from streamgram.server.exceptions import FIleNotFound, InvalidHash, LinkExpired
from streamgram import StartTime, __version__
from ..utils.time_format import get_readable_time
from ..utils.custom_dl import ByteStreamer, offset_fix, chunk_size
from streamgram.utils.render_template import render_page
from streamgram.vars import Var
from streamgram.utils.access import access_db
from streamgram.utils.link_expiry import link_expiry
from streamgram.utils.link_security import link_tokens
from streamgram.utils import audio_fix
from datetime import datetime

router = APIRouter()

# Root route for server status
@router.get("/", response_class=JSONResponse)
async def root_route_handler():
    logger.debug("Status endpoint requested")
    return JSONResponse(
        content={
            "server_status": "running",
            "uptime": get_readable_time(time.time() - StartTime),
            "telegram_bot": "@" + StreamBot.username,
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )

HASH_ID = re.compile(r"^([a-zA-Z0-9_-]{6})(\d+)$")
ID_ONLY = re.compile(r"^(\d+)(?:/.*)?$")
RANGE_RE = re.compile(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?", re.I)


def parse_path(request: Request, path: str):
    """Returns (message_id, secure_hash). Supports /<id>?hash=x, /<id>/<name>?hash=x and /<hash6><id>."""
    id_match = ID_ONLY.match(path)
    if id_match and request.query_params.get("hash"):
        return int(id_match.group(1)), request.query_params.get("hash")
    match = HASH_ID.match(path)
    if match:
        return int(match.group(2)), match.group(1)
    if id_match:
        logger.debug("parse_path(%r): id with no hash", path)
        return int(id_match.group(1)), None
    logger.debug("parse_path(%r): does not match any known pattern", path)
    raise HTTPException(status_code=400, detail="Invalid path format")


def parse_range(header, file_size):
    """Returns (start, end) inclusive for a valid single range, None to serve the whole file.
    Only the first range of a multi-range request is honoured. Raises 416 if unsatisfiable."""
    if not header:
        return None
    m = RANGE_RE.fullmatch(header)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        logger.debug("parse_range(%r): malformed, ignoring", header)
        return None  # malformed: ignore the header, as RFC 9110 allows
    first, last = m.group(1), m.group(2)
    unsatisfiable = HTTPException(
        status_code=416, detail="Range not satisfiable", headers={"Content-Range": f"bytes */{file_size}"}
    )
    if first == "":  # suffix range: last N bytes
        n = int(last)
        if n == 0 or file_size == 0:
            logger.debug("parse_range(%r): unsatisfiable suffix range (file_size=%s)", header, file_size)
            raise unsatisfiable
        result = max(file_size - n, 0), file_size - 1
        logger.debug("parse_range(%r) -> %s", header, result)
        return result
    start = int(first)
    end = min(int(last), file_size - 1) if last else file_size - 1
    if start >= file_size or start > end:
        logger.debug("parse_range(%r): unsatisfiable (file_size=%s)", header, file_size)
        raise unsatisfiable
    logger.debug("parse_range(%r) -> (%s, %s)", header, start, end)
    return start, end


@router.get("/watch/{path:path}", response_class=HTMLResponse)
async def watch_handler(request: Request, path: str):
    try:
        id, secure_hash = parse_path(request, path)
        if not secure_hash:
            raise InvalidHash
        return HTMLResponse(content=await render_page(id, secure_hash, request.query_params.get("audiofix")))
    except HTTPException:
        raise
    except InvalidHash as e:
        raise HTTPException(status_code=403, detail=e.message)
    except FIleNotFound as e:
        raise HTTPException(status_code=404, detail=e.message)
    except LinkExpired as e:
        raise HTTPException(status_code=410, detail=e.message)
    except Exception:
        logger.exception("Watch page failed for %s", path)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/{path:path}")
async def stream_handler(request: Request, path: str):
    try:
        id, secure_hash = parse_path(request, path)
        return await media_streamer(request, id, secure_hash)
    except HTTPException:
        raise
    except InvalidHash as e:
        raise HTTPException(status_code=403, detail=e.message)
    except FIleNotFound as e:
        raise HTTPException(status_code=404, detail=e.message)
    except LinkExpired as e:
        raise HTTPException(status_code=410, detail=e.message)
    except Exception:
        logger.exception("Streaming failed for %s", path)
        raise HTTPException(status_code=500, detail="Internal server error")


class_cache = {}


async def media_streamer(request: Request, id: int, secure_hash: str):
    # No hash -> reject before spending any Telegram API calls on scanner traffic.
    if not secure_hash:
        raise InvalidHash
    range_header = request.headers.get("range")
    # Get the index of the faster client
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]

    if Var.MULTI_CLIENT:
        logger.info(f"Client {index} is now serving {request.client.host}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logger.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logger.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    if await access_db.is_revoked(id):
        raise FIleNotFound
    file_id = await tg_connect.get_file_properties(id)

    if not await link_tokens.check(id, file_id.unique_id, secure_hash):
        logger.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash
    if await link_expiry.is_expired(id):
        raise LinkExpired

    # The audio-fix pass (streamgram/utils/audio_fix.py) reads this same endpoint via ffprobe/ffmpeg
    # over loopback to do its work; that self-call carries this header so it never recurses into
    # itself. A real browser/viewer request never sends it.
    is_internal_call = request.headers.get(audio_fix.INTERNAL_HEADER_NAME) == "1"
    # ?audiofix=1/true/on forces the transcode path on, ?audiofix=0/false/off forces it off,
    # regardless of auto-detection — the two comparison links the bot sends use these explicitly
    # so a viewer (or we, debugging) can tell "the original" and "the fixed" apart deterministically.
    force_audio_fix = audio_fix.parse_audiofix_override(request.query_params.get("audiofix"))
    # ?start=<seconds>: seeking with no disk cache means a seek just restarts the transcode from
    # the new point (streamgram/template/audiofix_player.html drives this); meaningless outside the
    # fix path, since plain passthrough already seeks natively via real byte ranges.
    try:
        start_seconds = max(0.0, float(request.query_params.get("start") or 0))
    except ValueError:
        start_seconds = 0.0
    # ?atrack=<N>: an explicit audio-track pick from the language selector on the seek-capable
    # player — always routes through the fixed pipeline for that track (see get_audio_fix_plan).
    try:
        atrack_raw = request.query_params.get("atrack")
        audio_track_override = int(atrack_raw) if atrack_raw is not None else None
    except ValueError:
        audio_track_override = None
    is_video_kind = (file_id.mime_type or "").split("/")[0] == "video"
    if not is_internal_call and is_video_kind and (
        audio_track_override is not None or force_audio_fix is not None or Var.ENABLE_AUDIO_FIX
    ):
        plan = await audio_fix.get_audio_fix_plan(
            id, secure_hash, force=force_audio_fix, audio_track_index=audio_track_override
        )
        if plan["needs_fix"]:
            try:
                audio_fix.begin_transcode()
            except audio_fix.TranscodeBusy:
                logger.warning(
                    "media_streamer(%s): audio-fix transcode slots full, falling back to plain "
                    "passthrough (video only, no audio)", id,
                )
            else:
                try:
                    proc = await audio_fix.start_ffmpeg_transcode(
                        id, secure_hash, plan["audio_track_index"], start_seconds=start_seconds
                    )
                except Exception:
                    logger.exception("media_streamer(%s): failed to start ffmpeg, falling back to passthrough", id)
                    audio_fix.end_transcode()
                else:
                    logger.info("media_streamer(%s): streaming with audio fixed (unsupported codec -> AAC)", id)
                    body = audio_fix.stream_ffmpeg_output(proc, request, id)
                    out_name = sanitize_header_value((file_id.file_name or "video").rsplit(".", 1)[0] + ".mp4")
                    headers = {
                        "Content-Type": "video/mp4",
                        "Content-Disposition": f'inline; filename="{out_name}"',
                        "Accept-Ranges": "none",
                    }
                    return StreamingResponse(body, status_code=200, headers=headers, media_type="video/mp4")

    file_size = file_id.file_size or 0
    byte_range = parse_range(range_header, file_size)

    if file_size == 0:
        logger.debug("media_streamer(%s): zero-byte file", id)
        return Response(status_code=200, headers={"Content-Length": "0", "Accept-Ranges": "bytes"})

    from_bytes, until_bytes = byte_range or (0, file_size - 1)
    req_length = until_bytes - from_bytes + 1
    new_chunk_size = await chunk_size(req_length)
    offset = await offset_fix(from_bytes, new_chunk_size)          # start rounded down to a chunk boundary
    first_part_cut = from_bytes - offset                            # bytes to drop from the first chunk
    last_part_cut = (until_bytes % new_chunk_size) + 1              # bytes to keep from the last chunk
    part_count = until_bytes // new_chunk_size - offset // new_chunk_size + 1

    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, new_chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    if str(file_id.file_type) == "FileType.PHOTO":
        mime_type = 'image/jpeg'
        timestamp = datetime.now().strftime("%d%m%Y%H%M%S")
        file_name = f"{timestamp}_{secrets.token_hex(2)}.jpg"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)[0] or "application/octet-stream"
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    file_name = sanitize_header_value(file_name)
    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(req_length),
        "Content-Disposition": f'{disposition}; filename="{file_name}"',
        "Accept-Ranges": "bytes",
    }
    if byte_range:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"
    logger.debug(f"Returning response for message with ID {id} bytes {from_bytes}-{until_bytes}.")
    return StreamingResponse(body, status_code=206 if byte_range else 200, headers=headers, media_type="application/octet-stream")

def sanitize_header_value(value):
    cleaned = re.sub(r'[\x00-\x1f\x7f"\\]', "", value.encode("ascii", errors="ignore").decode("ascii"))
    if cleaned != value:
        logger.debug("sanitize_header_value: %r -> %r", value, cleaned)
    return cleaned
