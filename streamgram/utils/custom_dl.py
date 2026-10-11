import math
import asyncio
import logging
logger = logging.getLogger("streamgram.utils.custom_dl")
# Concurrent in-flight GetFile calls yield_file() keeps ahead of the part being consumed.
# Derived from production logs of streaming a very large file: Telegram's media DC periodically
# answers at ~0.15 MB/s against the ~0.39 MB/s a video needs in real time (a 2.6x gap) -- fetching
# one part at a time pays that slowdown with nothing overlapping to hide it. 3x clears the gap
# with margin while keeping early-abandonment waste small and not bursting hard enough to look
# like abuse to Telegram's own throttling.
PREFETCH_WINDOW = 3
from streamgram.vars import Var
from typing import Dict, Union
from streamgram.bot import work_loads
from pyrogram import Client, utils, raw
from .file_properties import get_file_ids
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid
from streamgram.server.exceptions import FIleNotFound
from pyrogram.file_id import FileId, FileType, ThumbnailSource


async def chunk_size(length):
    length = max(length, 1)
    size = 2 ** max(min(math.ceil(math.log2(length / 1024)), 10), 2) * 1024
    logger.debug("chunk_size(%s) -> %s", length, size)
    return size


async def offset_fix(offset, chunksize):
    fixed = offset - offset % chunksize
    logger.debug("offset_fix(%s, %s) -> %s", offset, chunksize, fixed)
    return fixed


class ByteStreamer:
    def __init__(self, client: Client):
        """A custom class that holds the cache of a specific client and class functions.
        attributes:
            client: the client that the cache is for.
            cached_file_ids: a dict of cached file IDs.
            cached_file_properties: a dict of cached file properties.
        
        functions:
            generate_file_properties: returns the properties for a media of a specific message contained in Tuple.
            generate_media_session: returns the media session for the DC that contains the media file.
            yield_file: yield a file from telegram servers for streaming.
            
        This is a modified version of the <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/telegram/utils/custom_download.py>
        Thanks to Eyaadh <https://github.com/eyaadh>
        """
        self.clean_timer = 30 * 60
        self.client: Client = client
        self.cached_file_ids: Dict[int, FileId] = {}
        logger.debug("ByteStreamer created for client %r", client)
        asyncio.create_task(self.clean_cache())

    async def get_file_properties(self, id: int) -> FileId:
        """
        Returns the properties of a media of a specific message in a FIleId class.
        if the properties are cached, then it'll return the cached results.
        or it'll generate the properties from the Message ID and cache them.
        """
        if id not in self.cached_file_ids:
            await self.generate_file_properties(id)
            logger.debug(f"Cached file properties for message with ID {id}")
        return self.cached_file_ids[id]
    
    async def generate_file_properties(self, id: int) -> FileId:
        """
        Generates the properties of a media file on a specific message.
        returns ths properties in a FIleId class.
        """
        file_id = await get_file_ids(self.client, Var.BIN_CHANNEL, id)
        logger.debug(f"Generated file ID and Unique ID for message with ID {id}")
        if not file_id:
            logger.debug(f"Message with ID {id} not found")
            raise FIleNotFound
        self.cached_file_ids[id] = file_id
        logger.debug(f"Cached media message with ID {id}")
        return self.cached_file_ids[id]

    async def generate_media_session(self, client: Client, file_id: FileId) -> Session:
        """
        Generates the media session for the DC that contains the media file.
        This is required for getting the bytes from Telegram servers.
        """

        media_session = client.media_sessions.get(file_id.dc_id, None)

        if media_session is None:
            if file_id.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await Auth(
                        client, file_id.dc_id, await client.storage.test_mode()
                    ).create(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

                for _ in range(6):
                    exported_auth = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                    )

                    try:
                        await media_session.send(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        logger.debug(
                            f"Invalid authorization bytes for DC {file_id.dc_id}"
                        )
                        continue
                else:
                    await media_session.stop()
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await client.storage.auth_key(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()
            logger.debug(f"Created media session for DC {file_id.dc_id}")
            client.media_sessions[file_id.dc_id] = media_session
        else:
            logger.debug(f"Using cached media session for DC {file_id.dc_id}")
        return media_session


    @staticmethod
    async def get_location(file_id: FileId) -> Union[raw.types.InputPhotoFileLocation,
                                                     raw.types.InputDocumentFileLocation,
                                                     raw.types.InputPeerPhotoFileLocation,]:
        """
        Returns the file location for the media file.
        """
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        logger.debug("get_location: file_type=%s -> %s", file_type, type(location).__name__)
        return location

    async def yield_file(
        self,
        file_id: FileId,
        index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
    ) -> Union[str, None]:
        """
        Custom generator that yields the bytes of the media file.
        `offset` is chunk-aligned; the first part is trimmed by `first_part_cut` and the
        last part is trimmed to `last_part_cut` bytes, so exactly the requested range is sent.
        Keeps up to PREFETCH_WINDOW GetFile calls in flight on the same media session ahead of
        the part currently being consumed, so a slow reply from Telegram doesn't block everything
        behind it -- parts are always yielded strictly in order regardless of which one comes back
        first. Stopping early (a probe that only wants the first part or two, a client disconnect)
        only ever leaves at most PREFETCH_WINDOW already-in-flight calls to cancel, never the rest
        of a large range.
        Modded from <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/telegram/utils/custom_download.py#L20>
        Thanks to Eyaadh <https://github.com/eyaadh>
        """
        client = self.client
        work_loads[index] += 1
        current_part = 0
        pending: Dict[int, asyncio.Task] = {}
        logger.debug(f"Starting to yielding file with client {index}.")
        try:
            media_session = await self.generate_media_session(client, file_id)
            location = await self.get_location(file_id)

            def submit(part_number):
                part_offset = offset + (part_number - 1) * chunk_size
                pending[part_number] = asyncio.create_task(
                    media_session.send(
                        raw.functions.upload.GetFile(location=location, offset=part_offset, limit=chunk_size),
                    )
                )

            for part_number in range(1, min(PREFETCH_WINDOW, part_count) + 1):
                submit(part_number)

            for current_part in range(1, part_count + 1):
                r = await pending.pop(current_part)

                next_part = current_part + PREFETCH_WINDOW
                if next_part <= part_count:
                    submit(next_part)

                if not isinstance(r, raw.types.upload.File) or not r.bytes:
                    break
                start = first_part_cut if current_part == 1 else 0
                end = last_part_cut if current_part == part_count else len(r.bytes)
                yield r.bytes[start:end]
        except (TimeoutError, AttributeError) as e:
            logger.warning(f"Stream interrupted at part {current_part}/{part_count}: {e!r}")
        finally:
            for task in pending.values():
                task.cancel()
            for task in pending.values():
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.debug(f"Finished yielding file with {current_part} parts.")
            work_loads[index] -= 1

    async def clean_cache(self) -> None:
        """
        function to clean the cache to reduce memory usage
        """
        while True:
            await asyncio.sleep(self.clean_timer)
            self.cached_file_ids.clear()
            logger.debug("Cleaned the cache")