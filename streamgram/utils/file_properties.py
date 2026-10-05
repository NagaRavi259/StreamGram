import logging
from pyrogram import Client
from typing import Any, Optional
from pyrogram.types import Message
from pyrogram.file_id import FileId
from pyrogram.raw.types.messages import Messages
from streamgram.server.exceptions import FIleNotFound

logger = logging.getLogger("streamgram.utils.file_properties")


async def parse_file_id(message: "Message") -> Optional[FileId]:
    media = get_media_from_message(message)
    if media:
        return FileId.decode(media.file_id)
    logger.debug("parse_file_id: message %s has no media", getattr(message, "id", "?"))

async def parse_file_unique_id(message: "Messages") -> Optional[str]:
    media = get_media_from_message(message)
    if media:
        return media.file_unique_id
    logger.debug("parse_file_unique_id: message %s has no media", getattr(message, "id", "?"))

async def get_file_ids(client: Client, chat_id: int, id: int) -> Optional[FileId]:
    message = await client.get_messages(chat_id, id)
    if message.empty:
        logger.warning("get_file_ids: message %s/%s does not exist (empty)", chat_id, id)
        raise FIleNotFound
    media = get_media_from_message(message)
    if not media:
        logger.warning("get_file_ids: message %s/%s has no media", chat_id, id)
        raise FIleNotFound
    file_unique_id = await parse_file_unique_id(message)
    file_id = await parse_file_id(message)
    setattr(file_id, "file_size", getattr(media, "file_size", 0))
    setattr(file_id, "mime_type", getattr(media, "mime_type", ""))
    setattr(file_id, "file_name", getattr(media, "file_name", ""))
    setattr(file_id, "unique_id", file_unique_id)
    logger.debug("get_file_ids: resolved %s/%s (%s, %s bytes)", chat_id, id, file_id.mime_type, file_id.file_size)
    return file_id

def get_media_from_message(message: "Message") -> Any:
    media_types = (
        "audio",
        "document",
        "photo",
        "sticker",
        "animation",
        "video",
        "voice",
        "video_note",
    )
    for attr in media_types:
        media = getattr(message, attr, None)
        if media:
            return media
    logger.debug("get_media_from_message: no media attribute set on message %s", getattr(message, "id", "?"))


HASH_LENGTH = 12   # new links carry 12 characters of the unique id
MIN_HASH_LENGTH = 6  # links created before the change carry only 6 and keep working


def get_hash(media_msg: Message) -> str:
    media = get_media_from_message(media_msg)
    result = getattr(media, "file_unique_id", "")[:HASH_LENGTH]
    logger.debug("get_hash -> %s", result)
    return result


def hash_ok(unique_id: str, secure_hash) -> bool:
    """A link hash is valid if it is a long enough prefix of the file's unique id."""
    ok = bool(secure_hash) and len(secure_hash) >= MIN_HASH_LENGTH and unique_id.startswith(secure_hash)
    if not ok:
        logger.debug("hash_ok: %r is not a valid legacy hash for this file", secure_hash)
    return ok

def get_name(media_msg: Message) -> str:
    media = get_media_from_message(media_msg)
    file_name=getattr(media, "file_name", "")
    return file_name if file_name else ""

def get_media_file_size(m):
    media = get_media_from_message(m)
    return getattr(media, "file_size", 0)