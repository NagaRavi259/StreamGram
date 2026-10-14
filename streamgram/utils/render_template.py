from streamgram.vars import Var
from streamgram.bot import StreamBot
from streamgram.utils.human_readable import humanbytes
from streamgram.utils.file_properties import get_file_ids
from streamgram.utils.access import access_db
from streamgram.utils.link_expiry import link_expiry
from streamgram.utils.link_security import link_tokens
from streamgram.server.exceptions import InvalidHash, FIleNotFound, LinkExpired
import html
import json
import urllib.parse
import aiofiles
import logging
logger = logging.getLogger("streamgram.utils.render_template")


async def render_page(id, secure_hash):
    logger.debug("render_page(%s)", id)
    if await access_db.is_revoked(id):
        logger.info("render_page(%s): link is revoked", id)
        raise FIleNotFound
    file_data = await get_file_ids(StreamBot, int(Var.BIN_CHANNEL), int(id))
    if not await link_tokens.check(id, file_data.unique_id, secure_hash):
        logger.debug(f"Invalid hash for message with - ID {id}")
        raise InvalidHash
    if await link_expiry.is_expired(id):
        logger.info("render_page(%s): link has expired", id)
        raise LinkExpired
    raw_src = urllib.parse.urljoin(Var.URL, f'{int(id)}/?hash={urllib.parse.quote(secure_hash)}')
    src = html.escape(raw_src, quote=True)
    # File names are chosen by whoever uploaded the file: always escape before putting them in HTML.
    name = html.escape(file_data.file_name or "file")
    kind = (file_data.mime_type or "").split('/')[0].strip()
    if kind in ('video', 'audio'):
        heading = '{} {}'.format('Watch' if kind == 'video' else 'Listen', name)
        logger.debug("render_page(%s): media page (%s)", id, kind)

        if kind == "video":
            # Audio, video and subtitle tracks are all discovered and decoded client-side
            # (streamgram/template/player.html, via ArtPlayer + Mediabunny) — no server-side probe
            # or transcode needed any more, so there's nothing to compute here beyond the plain
            # stream URL. Tuple order must match the placeholders' left-to-right order in the
            # template file: Python's %-formatting is purely positional against the text.
            logger.debug("render_page(%s): using the client-side player", id)
            async with aiofiles.open('streamgram/template/player.html', encoding='utf-8') as r:
                page = (await r.read()) % (heading, heading, json.dumps(raw_src))
        else:
            async with aiofiles.open('streamgram/template/req.html', encoding='utf-8') as r:
                page = (await r.read()).replace('__MEDIA__', kind) % (heading, name, src)
    else:
        heading = 'Download {}'.format(name)
        logger.debug("render_page(%s): download page", id)
        async with aiofiles.open('streamgram/template/dl.html', encoding='utf-8') as r:
            page = (await r.read()) % (heading, name, src, humanbytes(int(file_data.file_size or 0)))
    return page
