import logging
from streamgram.bot import StreamBot
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import filters
import time
import shutil, psutil
from streamgram.vars import Var
from streamgram.utils.human_readable import humanbytes
from streamgram.utils.time_format import get_readable_time
from streamgram import StartTime

logger = logging.getLogger("streamgram.bot.plugins.extra")


@StreamBot.on_message(filters.command(['stats', 'status']) & filters.private & filters.user(list(Var.OWNER_ID)))
async def stats(bot, update):
  logger.info("/stats requested by owner %s", getattr(getattr(update, "from_user", None), "id", "?"))
  currentTime = get_readable_time(time.time() - StartTime)
  total, used, free = shutil.disk_usage('.')
  total = humanbytes(total)
  used = humanbytes(used)
  free = humanbytes(free)
  sent = humanbytes(psutil.net_io_counters().bytes_sent)
  recv = humanbytes(psutil.net_io_counters().bytes_recv)
  cpuUsage = psutil.cpu_percent(interval=0.5)
  memory = psutil.virtual_memory().percent
  disk = psutil.disk_usage('/').percent
  botstats = f'<b>Bot Uptime:</b> {currentTime}\n' \
            f'<b>Total disk space:</b> {total}\n' \
            f'<b>Used:</b> {used}  ' \
            f'<b>Free:</b> {free}\n\n' \
            f'📊Data Usage📊\n<b>Upload:</b> {sent}\n' \
            f'<b>Down:</b> {recv}\n\n' \
            f'<b>CPU:</b> {cpuUsage}% ' \
            f'<b>RAM:</b> {memory}% ' \
            f'<b>Disk:</b> {disk}%'
  logger.debug("/stats: cpu=%s%% mem=%s%% disk=%s%%", cpuUsage, memory, disk)
  await update.reply_text(botstats)
