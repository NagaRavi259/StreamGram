# Taken from megadlbot_oss <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/webserver/routes.py>
# Thanks to Eyaadh <https://github.com/eyaadh>

import re
import time
import math
import traceback
import logging
import secrets
import mimetypes
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from streamgram.bot import multi_clients, work_loads, StreamBot
from streamgram.server.exceptions import FIleNotFound, InvalidHash
from streamgram import StartTime, __version__
from ..utils.time_format import get_readable_time
from ..utils.custom_dl import ByteStreamer, offset_fix, chunk_size
from streamgram.utils.render_template import render_page
from streamgram.vars import Var
from datetime import datetime

router = APIRouter()

# Root route for server status
@router.get("/", response_class=JSONResponse)
async def root_route_handler():
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

@router.get("/watch/{path:path}", response_class=HTMLResponse)
async def stream_handler(request: Request, path: str):
    try:
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:/\S+)?", path).group(1))
            secure_hash = request.query_params.get("hash")
        return HTMLResponse(content=await render_page(id, secure_hash))
    except InvalidHash as e:
        raise HTTPException(status_code=403, detail=e.message)
    except FIleNotFound as e:
        raise HTTPException(status_code=404, detail=e.message)
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{path:path}")
async def stream_handler(request: Request, path: str):
    try:
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            # id = int(re.search(r"(\d+)(?:/\S+)?", path).group(1))
            match = re.search(r"(\d+)(?:/(\S+))?", path)
            if match:
                id = int(match.group(1))
                secure_hash = request.query_params.get("hash")
            else:
                raise HTTPException(status_code=400, detail="Invalid path format")
            # secure_hash = request.query_params.get("hash")
        return await media_streamer(request, id, secure_hash)
    except InvalidHash as e:
        raise HTTPException(status_code=403, detail=e.message)
    except FIleNotFound as e:
        raise HTTPException(status_code=404, detail=e.message)
    except Exception as e:
        print(e)
        # Get the traceback and extract the line number
        traceback_details = traceback.format_exc()
        print(f"Traceback details:\n{traceback_details}")
        logging.critical(e.with_traceback(None))
        raise HTTPException(status_code=500, detail=str(e))

class_cache = {}

async def media_streamer(request: Request, id: int, secure_hash: str):
    range_header = request.headers.get("range", 0)
    # Get the index of the faster client
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]

    if Var.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.client.host}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    file_id = await tg_connect.get_file_properties(id)

    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash

    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = 0
        until_bytes = file_size - 1

    req_length = until_bytes - from_bytes
    new_chunk_size = await chunk_size(req_length)
    offset = await offset_fix(from_bytes, new_chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = (until_bytes % new_chunk_size) + 1
    part_count = math.ceil(req_length / new_chunk_size)

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
            mime_type = mimetypes.guess_type(file_id.file_name)[0]
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    file_name = sanitize_header_value(file_name)
    headers = {
        "Content-Type": mime_type,
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Disposition": f'{disposition}; filename="{file_name}"',
        "Accept-Ranges": "bytes",
    }
    logging.debug(f"Returning response for message with ID {id} and range header.")
    return StreamingResponse(body, status_code=206 if range_header else 200, headers=headers, media_type="application/octet-stream")

def sanitize_header_value(value):
    return value.encode("ascii", errors="ignore").decode("ascii")
