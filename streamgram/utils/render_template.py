from streamgram.vars import Var
from streamgram.bot import StreamBot
from streamgram.utils.human_readable import humanbytes
from streamgram.utils.file_properties import get_file_ids
from streamgram.utils.access import access_db
from streamgram.utils.link_expiry import link_expiry
from streamgram.utils.link_security import link_tokens
from streamgram.utils import audio_fix
from streamgram.server.exceptions import InvalidHash, FIleNotFound, LinkExpired
import html
import json
import urllib.parse
import aiofiles
import logging
logger = logging.getLogger("streamgram.utils.render_template")


async def render_page(id, secure_hash, audiofix_param=None):
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

        plan = None
        media_info = None
        if kind == "video":
            force = audio_fix.parse_audiofix_override(audiofix_param)
            if force is not None or Var.ENABLE_AUDIO_FIX:
                plan = await audio_fix.get_audio_fix_plan(id, secure_hash, force=force)
                if plan["needs_fix"]:
                    media_info = await audio_fix.probe_media_info(id, secure_hash)
        needs_seek_player = bool(plan and plan["needs_fix"] and media_info and media_info["duration"] is not None)

        if needs_seek_player:
            # The stream itself can't report its own total duration or support real byte-range
            # seeking (it's re-transcoded per request, no disk cache — see audio_fix.py), so this
            # page gets its own player that tracks duration/position itself and seeks by
            # reloading the stream at a new `start=` point (streamgram/template/audiofix_player.html).
            # It also gets a language/track picker from the same probe, wired to `&atrack=N`.
            logger.debug("render_page(%s): using the seek-capable audio-fix player", id)
            # &audiofix=1 pinned explicitly so every reload this page does (every seek, every
            # track switch) stays on the fixed path, regardless of what triggered it here.
            stream_url_for_js = f"{raw_src}&audiofix=1"
            async with aiofiles.open('streamgram/template/audiofix_player.html', encoding='utf-8') as r:
                page = (await r.read()) % (
                    heading, heading,
                    json.dumps(media_info["duration"]),
                    json.dumps(stream_url_for_js),
                    json.dumps(media_info["audio_tracks"]),
                    json.dumps(plan["audio_track_index"]),
                )
        else:
            async with aiofiles.open('streamgram/template/req.html', encoding='utf-8') as r:
                page = (await r.read()).replace('__MEDIA__', kind) % (heading, name, src)
    else:
        heading = 'Download {}'.format(name)
        logger.debug("render_page(%s): download page", id)
        async with aiofiles.open('streamgram/template/dl.html', encoding='utf-8') as r:
            page = (await r.read()) % (heading, name, src, humanbytes(int(file_data.file_size or 0)))
    return page
