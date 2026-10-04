# © agrprojects
import asyncio
import logging
import os
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from ..utils.request_log import RequestLog
from ..vars import Var
from .stream_routes import router

logger = logging.getLogger("streamgram.server")

# Every web request is appended to <LOG_DIR>/request_logs.csv (current month); older months are rotated
# into request_logs_YYYY-MM.csv and nothing is deleted.
request_log = RequestLog(os.path.join(Var.LOG_DIR, "request_logs.csv"))
try:
    request_log.migrate()  # one-time split of an old multi-month file
    request_log.ensure()
except Exception:
    logger.exception("Could not prepare the request log")

# Initialize FastAPI app
app = FastAPI(docs_url=None, redoc_url=None)

# Middleware for request logging
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    # Capture the details
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    ip_address = request.client.host
    endpoint = str(request.url)

    # Log to the CSV file (in a worker thread so the event loop is never blocked); a logging
    # problem must never fail the request itself.
    try:
        await asyncio.to_thread(request_log.append, [timestamp, ip_address, endpoint])
    except Exception:
        logger.exception("Could not write the request log")

    # Call the next middleware/handler
    response = await call_next(request)
    return response

# Define the route to serve the favicon.ico
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Adjust the file path to where your favicon.ico is located
    return FileResponse("streamgram/favicon.ico")

# Include routes from stream_routes
app.include_router(router)
