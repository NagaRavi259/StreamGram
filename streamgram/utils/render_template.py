from streamgram.vars import Var
from streamgram.bot import StreamBot
from streamgram.utils.human_readable import humanbytes
from streamgram.utils.file_properties import get_file_ids
from streamgram.server.exceptions import InvalidHash
import html
import urllib.parse
import aiofiles
import logging


async def render_page(id, secure_hash):
    file_data = await get_file_ids(StreamBot, int(Var.BIN_CHANNEL), int(id))
    if file_data.unique_id[:6] != secure_hash:
        logging.debug(f'link hash: {secure_hash} - {file_data.unique_id[:6]}')
        logging.debug(f"Invalid hash for message with - ID {id}")
        raise InvalidHash
    src = html.escape(urllib.parse.urljoin(Var.URL, f'{secure_hash}{str(id)}'), quote=True)
    # File names are chosen by whoever uploaded the file: always escape before putting them in HTML.
    name = html.escape(file_data.file_name or "file")
    kind = (file_data.mime_type or "").split('/')[0].strip()
    if kind in ('video', 'audio'):
        heading = '{} {}'.format('Watch' if kind == 'video' else 'Listen', name)
        async with aiofiles.open('streamgram/template/req.html') as r:
            page = (await r.read()).replace('tag', kind) % (heading, name, src)
    else:
        heading = 'Download {}'.format(name)
        async with aiofiles.open('streamgram/template/dl.html', encoding='utf-8') as r:
            page = (await r.read()) % (heading, name, src, humanbytes(int(file_data.file_size or 0)))
    return page
