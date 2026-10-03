"""Owner admin menu (/admin), user access requests, invites, access groups and limits."""
import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote

from pyrogram import Client, filters, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton as B, Message, CallbackQuery

from streamgram.bot import StreamBot
from streamgram.vars import Var
from streamgram.utils.access import (
    access_db, is_exempt, profile_of, describe_quota, notify_owners, PERIODS, PERIOD_LABELS, ACCESS_STATUSES,
)

PER_PAGE = 8
HTML = enums.ParseMode.HTML
owner_only = filters.user(list(Var.OWNER_ID))
inputs = {}  # owner_id -> pending text-input state


# ---------------------------------------------------------------- helpers
def when(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "-"


def ago(ts):
    if not ts:
        return "never used"
    d = int(time.time()) - ts
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= size:
            return f"{d // size}{unit} ago"
    return "just now"


def who(rec_or_id, uid=None):
    """HTML mention for a user doc."""
    if isinstance(rec_or_id, dict):
        uid = rec_or_id["id"]
        name = rec_or_id.get("first_name") or (("@" + rec_or_id["username"]) if rec_or_id.get("username") else str(uid))
    else:
        uid, name = rec_or_id, str(rec_or_id)
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


async def edit(cq: CallbackQuery, text, rows):
    try:
        await cq.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=HTML, disable_web_page_preview=True
        )
    except MessageNotModified:
        pass


def pager(prefix, page, total):
    row = []
    if page > 0:
        row.append(B("◀ Prev", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * PER_PAGE < total:
        row.append(B("Next ▶", callback_data=f"{prefix}:{page + 1}"))
    return [row] if row else []


HOME_BTN = [B("🏠 Menu", callback_data="adm:home")]


# ---------------------------------------------------------------- startup
asyncio.get_event_loop().create_task(access_db.init())


_backfill_running = False


async def backfill_join_requests():
    """Record join requests that were already pending. Retries with back-off until every access
    chat succeeded: the first attempt fails while the bot cannot yet see the chat."""
    global _backfill_running
    if _backfill_running:
        return
    _backfill_running = True
    try:
        done, delay = set(), 30
        while True:
            todo = [c for c in await access_db.group_ids() if c not in done]
            if not todo:
                return
            for chat_id in todo:
                try:
                    n = 0
                    async for r in StreamBot.get_chat_join_requests(chat_id):
                        await access_db.add_join_request(r.user.id)
                        n += 1
                    done.add(chat_id)
                    logging.info(f"Backfilled {n} pending join requests from {chat_id}")
                except Exception as e:
                    logging.warning(f"Could not backfill join requests for {chat_id} (retrying in {delay}s): {e}")
            if all(c in done for c in todo):
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, 900)
    finally:
        _backfill_running = False


asyncio.get_event_loop().create_task(backfill_join_requests())


# ---------------------------------------------------------------- tracking: join requests & groups
async def _is_access_group(_, __, update):
    return update.chat.id in await access_db.group_ids()


@StreamBot.on_chat_join_request(filters.create(_is_access_group))
async def on_join_request(b: Client, r):
    """Anyone asking to join an access group gets access by default.
    The bot must be an admin of the group with the 'invite users' right to receive these."""
    await access_db.add_join_request(r.from_user.id)
    await access_db.log(r.from_user.id, "join_request", 0, str(r.chat.id))


@StreamBot.on_chat_member_updated(group=7)
async def track_bot_chats(b: Client, u):
    """Remember groups/channels the bot is added to or removed from."""
    me = u.new_chat_member or u.old_chat_member
    if not me or not me.user or not me.user.is_self or u.chat.type == enums.ChatType.PRIVATE:
        return
    if u.new_chat_member is None or u.new_chat_member.status in (
        enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED
    ):
        await access_db.forget_chat(u.chat.id)
    else:
        await access_db.note_chat(u.chat)


_seen_chats = set()


@StreamBot.on_message(filters.group, group=7)
async def see_group(b: Client, m: Message):
    """Cheap fallback: learn about groups the bot was already in before this feature existed."""
    if m.chat.id not in _seen_chats:
        _seen_chats.add(m.chat.id)
        await access_db.note_chat(m.chat)


# ---------------------------------------------------------------- user: request access
@StreamBot.on_callback_query(filters.regex(r"^req:access$"))
async def request_access(c: Client, cq: CallbackQuery):
    user = cq.from_user
    if is_exempt(user.id):
        return await cq.answer("You already have access.", show_alert=True)
    rec = await access_db.get_user(user.id)
    status = rec.get("status") if rec else None
    if status == "banned":
        return await cq.answer("You are banned from this bot.", show_alert=True)
    if status == "approved":
        return await cq.answer("You already have access. Send /start.", show_alert=True)
    if status == "pending":
        return await cq.answer("Your request is already waiting for approval.", show_alert=True)
    await access_db.set_status(user.id, "pending", 0, profile_of(user), note="requested via button")
    await cq.answer("Request sent ✅")
    try:
        await cq.message.edit_text("⏳ **Request sent.** You will be notified when the admin decides.")
    except Exception:
        pass
    uname = f" (@{user.username})" if user.username else ""
    await notify_owners(
        c,
        f"🔔 <b>Access request</b>\n\n{who({'id': user.id, 'first_name': user.first_name})}{uname}\n"
        f"ID: <code>{user.id}</code>",
        decision_markup(user.id),
    )


def decision_markup(uid):
    return InlineKeyboardMarkup([
        [B("✅ Approve", callback_data=f"adm:apr:{uid}"), B("❌ Reject", callback_data=f"adm:rej:{uid}")],
        [B("🚫 Ban", callback_data=f"adm:ban:{uid}")],
    ])


# ---------------------------------------------------------------- invites: accept in chat
@StreamBot.on_callback_query(filters.regex(r"^inv:acc:"))
async def accept_invite(c: Client, cq: CallbackQuery):
    token = cq.data.split(":", 2)[2]
    ok = await redeem(c, cq.from_user, token)
    await cq.answer("Access granted ✅" if ok else "This invite is invalid or expired.", show_alert=not ok)
    if ok:
        try:
            await cq.message.edit_text("✅ **Invite accepted.** Send me any file to get a link.")
        except Exception:
            pass


async def redeem(c: Client, user, token) -> bool:
    """Used by the 'Accept' button and by the /start inv_<token> deep link (see start_help)."""
    rec = await access_db.get_user(user.id)
    if rec and rec.get("status") == "banned":
        return False
    inv = await access_db.redeem_invite(token, user.id)
    if not inv:
        return False
    await access_db.set_status(user.id, "approved", inv["by"], profile_of(user), source="invite", note="invite accepted")
    await notify_owners(c, f"📨 {who({'id': user.id, 'first_name': user.first_name})} accepted an invite.")
    return True


# ---------------------------------------------------------------- actions (approve / reject / ban ...)
ACTIONS = {
    # action: (new status, text to user or None)
    "apr": ("approved", "✅ Your access request was <b>approved</b>. Send /start to begin."),
    "rej": ("rejected", "❌ Your access request was <b>rejected</b>."),
    "ban": ("banned", "🚫 You have been <b>banned</b> from this bot."),
    "unban": ("revoked", None),
    "rev": ("revoked", "Your access to this bot was <b>revoked</b>."),
}
OUTCOME = {"apr": "✅ Approved", "rej": "❌ Rejected", "ban": "🚫 Banned", "unban": "♻️ Unbanned", "rev": "⛔ Revoked"}


async def apply_action(c: Client, owner_id, action, uid):
    status, to_user = ACTIONS[action]
    source = "manual" if action == "apr" else None
    await access_db.set_status(uid, status, owner_id, source=source, note=f"by owner {owner_id}")
    if action == "ban":
        await access_db.remove_join_request(uid)
    if to_user:
        try:
            await c.send_message(uid, to_user, parse_mode=HTML)
        except Exception as e:
            logging.info(f"Could not notify user {uid}: {e}")


@StreamBot.on_callback_query(filters.regex(r"^adm:(apr|rej|ban|unban|rev):\d+$") & owner_only)
async def on_action(c: Client, cq: CallbackQuery):
    _, action, uid = cq.data.split(":")
    uid = int(uid)
    rec = await access_db.get_user(uid) or {"id": uid}
    # request notifications carry only decision buttons -> replace them with the outcome
    is_notification = cq.message.text and cq.message.text.startswith("🔔")
    already = rec.get("status") == ACTIONS[action][0]
    if not already:
        await apply_action(c, cq.from_user.id, action, uid)
    await cq.answer(OUTCOME[action] + (" (already)" if already else ""))
    if is_notification:
        rec = await access_db.get_user(uid) or rec
        await edit(cq, f"{OUTCOME[action]}: {who(rec)} (<code>{uid}</code>)\nby {html.escape(cq.from_user.first_name)}", [])
    else:
        await show_user(cq, uid)


# ---------------------------------------------------------------- /admin menu
async def home_view():
    pending = await access_db.count_users(("pending",))
    approved = await access_db.count_users(ACCESS_STATUSES)
    period, limit = await access_db.get_default_quota()
    text = (
        "🛠 <b>Admin menu</b>\n\n"
        f"Users with access: <b>{approved}</b>\n"
        f"Pending requests: <b>{pending}</b>\n"
        f"Default limit: <b>{describe_quota(period, limit)}</b>"
    )
    rows = [
        [B("📨 Invite a person", callback_data="adm:inv"), B(f"👥 Users ({approved})", callback_data="adm:users:0")],
        [B(f"⏳ Pending ({pending})", callback_data="adm:pend:0"), B("🏘 Groups", callback_data="adm:grp")],
        [B("📊 Default limit", callback_data="adm:q:0"), B("📜 History", callback_data="adm:hist:0")],
        [B("✖ Close", callback_data="adm:close")],
    ]
    return text, rows


@StreamBot.on_message(filters.command("admin") & filters.private & owner_only)
async def admin_cmd(c: Client, m: Message):
    inputs.pop(m.from_user.id, None)
    text, rows = await home_view()
    await m.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=HTML)


@StreamBot.on_callback_query(filters.regex(r"^adm:home$") & owner_only)
async def cb_home(c, cq):
    inputs.pop(cq.from_user.id, None)
    text, rows = await home_view()
    await edit(cq, text, rows)


@StreamBot.on_callback_query(filters.regex(r"^adm:close$") & owner_only)
async def cb_close(c, cq):
    inputs.pop(cq.from_user.id, None)
    await cq.message.delete()


# ---- users list (recent -> old) and detail
async def users_view(statuses, page, title, prefix, sort_field):
    total = await access_db.count_users(statuses)
    users = await access_db.list_users(statuses, page * PER_PAGE, PER_PAGE, sort_field)
    lines = [title.format(total=total)]
    rows = []
    for u in users:
        label = u.get("first_name") or (("@" + u["username"]) if u.get("username") else str(u["id"]))
        stamp = ago(u.get(sort_field)) if sort_field == "last_used" else when(u.get(sort_field))[:10]
        rows.append([B(f"{label[:22]} · {stamp}", callback_data=f"adm:u:{u['id']}")])
    if not users:
        lines.append("\nNobody yet.")
    rows += pager(prefix, page, total)
    rows.append(HOME_BTN)
    return "\n".join(lines), rows


@StreamBot.on_callback_query(filters.regex(r"^adm:users:\d+$") & owner_only)
async def cb_users(c, cq):
    page = int(cq.data.split(":")[2])
    await edit(cq, *await users_view(
        ACCESS_STATUSES, page, "👥 Users with access (<b>{total}</b>), most recently active first", "adm:users", "last_used"))


@StreamBot.on_callback_query(filters.regex(r"^adm:pend:\d+$") & owner_only)
async def cb_pending(c, cq):
    page = int(cq.data.split(":")[2])
    await edit(cq, *await users_view(
        ("pending",), page, "⏳ Pending requests (<b>{total}</b>), newest first", "adm:pend", "requested_at"))


async def show_user(cq, uid):
    rec = await access_db.get_user(uid)
    if not rec:
        return await edit(cq, "User not found.", [HOME_BTN])
    status = rec.get("status", "-")
    period, limit = await access_db.effective_quota(uid)
    own = rec.get("quota")
    usage = []
    for p in ("hour", "day", "week", "month", "life"):
        usage.append(f"{PERIOD_LABELS[p]}: {await access_db.count_usage(uid, p)}")
    text = (
        f"👤 {who(rec)}"
        f"{' (@' + rec['username'] + ')' if rec.get('username') else ''}\n"
        f"ID: <code>{uid}</code>\n"
        f"Status: <b>{status}</b>\n"
        f"Last used the bot: <b>{ago(rec.get('last_used'))}</b> ({when(rec.get('last_used'))})\n"
        f"Access: {'via group membership / join request' if status == 'group' else 'granted ' + when(rec.get('granted_at')) + (' via ' + rec['source'] if rec.get('source') else '')}\n"
        f"Last update: {when(rec.get('updated_at'))}\n"
        f"Limit: <b>{describe_quota(period, limit)}</b> ({'personal' if own else 'default'})\n"
        f"Links generated — " + " · ".join(usage)
    )
    if status == "group":
        first = [B("🚫 Ban", callback_data=f"adm:ban:{uid}")]  # access comes from the group; only a ban overrides it
    elif status == "approved":
        first = [B("⛔ Revoke", callback_data=f"adm:rev:{uid}"), B("🚫 Ban", callback_data=f"adm:ban:{uid}")]
    elif status == "banned":
        first = [B("♻️ Unban", callback_data=f"adm:unban:{uid}")]
    else:
        first = [B("✅ Approve", callback_data=f"adm:apr:{uid}"), B("🚫 Ban", callback_data=f"adm:ban:{uid}")]
    rows = [
        first,
        [B("📊 Set limit", callback_data=f"adm:q:{uid}"), B("📜 History", callback_data=f"adm:uh:{uid}:0")],
        [B("◀ Users", callback_data="adm:users:0"), *HOME_BTN],
    ]
    await edit(cq, text, rows)


@StreamBot.on_callback_query(filters.regex(r"^adm:u:\d+$") & owner_only)
async def cb_user(c, cq):
    await show_user(cq, int(cq.data.split(":")[2]))


# ---- history
def history_text(items, total, title):
    lines = [f"{title} ({total})\n"]
    for h in items:
        by = f" by <code>{h['by']}</code>" if h.get("by") else ""
        lines.append(
            f"{when(h['ts'])} · <b>{h['action']}</b> · {html.escape(h.get('name') or '')} "
            f"<code>{h['uid']}</code>{by}"
        )
    if not items:
        lines.append("Nothing yet.")
    return "\n".join(lines)


@StreamBot.on_callback_query(filters.regex(r"^adm:hist:\d+$") & owner_only)
async def cb_history(c, cq):
    page = int(cq.data.split(":")[2])
    items, total = await access_db.get_history(page * PER_PAGE, PER_PAGE)
    await edit(cq, history_text(items, total, "📜 History, newest first"), [*pager("adm:hist", page, total), HOME_BTN])


@StreamBot.on_callback_query(filters.regex(r"^adm:uh:\d+:\d+$") & owner_only)
async def cb_user_history(c, cq):
    _, _, uid, page = cq.data.split(":")
    uid, page = int(uid), int(page)
    items, total = await access_db.get_history(page * PER_PAGE, PER_PAGE, uid)
    rows = pager(f"adm:uh:{uid}", page, total)
    rows.append([B("◀ User", callback_data=f"adm:u:{uid}"), *HOME_BTN])
    await edit(cq, history_text(items, total, "📜 User history"), rows)


# ---- limits (per user, or default when uid == 0)
@StreamBot.on_callback_query(filters.regex(r"^adm:q:\d+$") & owner_only)
async def cb_quota(c, cq):
    uid = int(cq.data.split(":")[2])
    target = "everyone (default)" if uid == 0 else f"user <code>{uid}</code>"
    period, limit = await (access_db.get_default_quota() if uid == 0 else access_db.effective_quota(uid))
    text = (
        f"📊 <b>Link limit</b> for {target}\nCurrent: <b>{describe_quota(period, limit)}</b>\n\n"
        "Choose the period. Windows are rolling (e.g. 'day' = last 24 hours)."
    )
    rows = [
        [B(p.capitalize(), callback_data=f"adm:qp:{uid}:{p}") for p in ("hour", "day", "week", "month")],
        [B("Lifetime total", callback_data=f"adm:qp:{uid}:life"), B("♾ Unlimited", callback_data=f"adm:qp:{uid}:unlimited")],
    ]
    if uid:
        rows.append([B("↩ Use default", callback_data=f"adm:qp:{uid}:default")])
        rows.append([B("◀ User", callback_data=f"adm:u:{uid}"), *HOME_BTN])
    else:
        rows.append(HOME_BTN)
    await edit(cq, text, rows)


@StreamBot.on_callback_query(filters.regex(r"^adm:qp:\d+:\w+$") & owner_only)
async def cb_quota_period(c, cq):
    _, _, uid, period = cq.data.split(":")
    uid = int(uid)
    if period in ("unlimited", "default"):
        if uid == 0:
            await access_db.set_default_quota("unlimited", None)
        elif period == "default":
            await access_db.set_user_quota(uid, None, None)
        else:
            await access_db.set_user_quota(uid, "unlimited", None)
        await access_db.log(uid or 0, f"limit_{period}", cq.from_user.id)
        await cq.answer("Saved ✅")
        return await (show_user(cq, uid) if uid else cb_home(c, cq))
    inputs[cq.from_user.id] = {"kind": "quota", "uid": uid, "period": period}
    await edit(
        cq,
        f"Send the number of links allowed per <b>{PERIOD_LABELS[period]}</b> (e.g. <code>20</code>).\n"
        "Send /cancel to abort.",
        [[B("✖ Cancel", callback_data="adm:home")]],
    )


# ---- invites
@StreamBot.on_callback_query(filters.regex(r"^adm:inv$") & owner_only)
async def cb_invite(c, cq):
    inputs.pop(cq.from_user.id, None)
    await edit(
        cq,
        "📨 <b>Invite a person</b>\n\n• <b>By username / ID</b>: the bot DMs them an invite with an Accept button "
        "(works only if they have started the bot; otherwise you get a link to send yourself).\n"
        "• <b>Create link</b>: a single-use link valid for 7 days that you can share anywhere.",
        [
            [B("👤 By username / ID", callback_data="adm:invu"), B("🔗 Create link", callback_data="adm:invl")],
            HOME_BTN,
        ],
    )


def invite_link(token):
    return f"https://t.me/{StreamBot.username}?start=inv_{token}"


def share_markup(link, extra=None):
    share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote("You're invited to use this bot:")
    return [[B("📤 Share", url=share)], *(extra or []), [B("◀ Invite", callback_data="adm:inv"), *HOME_BTN]]


@StreamBot.on_callback_query(filters.regex(r"^adm:invl$") & owner_only)
async def cb_invite_link(c, cq):
    token = await access_db.create_invite(cq.from_user.id)
    await access_db.log(0, "invite_link_created", cq.from_user.id, token)
    link = invite_link(token)
    await edit(cq, f"🔗 Single-use invite link (valid 7 days):\n\n<code>{link}</code>", share_markup(link))


@StreamBot.on_callback_query(filters.regex(r"^adm:invu$") & owner_only)
async def cb_invite_user(c, cq):
    inputs[cq.from_user.id] = {"kind": "invite_user"}
    await edit(cq, "Send the person's <b>@username</b> or numeric <b>user ID</b>.\n/cancel to abort.",
               [[B("✖ Cancel", callback_data="adm:inv")]])


# ---- groups
@StreamBot.on_callback_query(filters.regex(r"^adm:grp$") & owner_only)
async def cb_groups(c, cq):
    inputs.pop(cq.from_user.id, None)
    rows, lines = [], ["🏘 <b>Access groups</b>\nMembers of these chats (and people who asked to join) get access. "
                       "The bot only reads membership; it never posts there.\n"]

    async def health(chat_id):
        ok, detail = await access_db.chat_visibility(c, chat_id)
        return f"✅ bot is {detail}" if ok else f"⚠️ bot can't see this chat ({detail}) — add it as an admin"

    if Var.USER_GROUP_ID < 0:
        lines.append(f"• <code>{Var.USER_GROUP_ID}</code> (from config) — {await health(Var.USER_GROUP_ID)}")
    for ch in await access_db.list_chats(True):
        lines.append(f"• {html.escape(ch['title'])} <code>{ch['chat_id']}</code> — {await health(ch['chat_id'])}")
        rows.append([B(f"🗑 Remove {ch['title'][:22]}", callback_data=f"adm:grm:{ch['chat_id']}")])
    if len(lines) == 1:
        lines.append("None configured.")
    rows.append([B("➕ Pick from bot's groups", callback_data="adm:gadd"), B("🔢 Add by ID", callback_data="adm:gm")])
    rows.append(HOME_BTN)
    await edit(cq, "\n".join(lines), rows)


@StreamBot.on_callback_query(filters.regex(r"^adm:gadd$") & owner_only)
async def cb_group_pick(c, cq):
    chats = await access_db.list_chats(False)
    rows = [[B(ch["title"][:40], callback_data=f"adm:gp:{ch['chat_id']}")] for ch in chats]
    text = "Pick a chat the bot has joined:" if chats else (
        "No other chats known yet. The bot learns about chats when it is added or sees a message there. "
        "You can also add one by ID."
    )
    rows.append([B("◀ Groups", callback_data="adm:grp")])
    await edit(cq, text, rows)


@StreamBot.on_callback_query(filters.regex(r"^adm:gp:-?\d+$") & owner_only)
async def cb_group_enable(c, cq):
    await access_db.set_chat_enabled(int(cq.data.split(":")[2]), True)
    await access_db.log(0, "group_added", cq.from_user.id, cq.data.split(":")[2])
    await cq.answer("Group added ✅")
    asyncio.get_event_loop().create_task(backfill_join_requests())
    await cb_groups(c, cq)


@StreamBot.on_callback_query(filters.regex(r"^adm:grm:-?\d+$") & owner_only)
async def cb_group_remove(c, cq):
    await access_db.set_chat_enabled(int(cq.data.split(":")[2]), False)
    await access_db.log(0, "group_removed", cq.from_user.id, cq.data.split(":")[2])
    await cq.answer("Group removed")
    await cb_groups(c, cq)


@StreamBot.on_callback_query(filters.regex(r"^adm:gm$") & owner_only)
async def cb_group_manual(c, cq):
    inputs[cq.from_user.id] = {"kind": "group_id"}
    await edit(cq, "Send the group's numeric ID (like <code>-100123…</code>) or @username. The bot must already be in it.\n/cancel to abort.",
               [[B("✖ Cancel", callback_data="adm:grp")]])


# ---------------------------------------------------------------- owner text input
@StreamBot.on_message(filters.private & filters.text & owner_only, group=5)
async def owner_input(c: Client, m: Message):
    state = inputs.get(m.from_user.id)
    if not state:
        return
    text = m.text.strip()
    if text == "/cancel":
        inputs.pop(m.from_user.id)
        return await m.reply_text("Cancelled. /admin")
    if text.startswith("/"):
        return
    kind = state["kind"]
    try:
        if kind == "quota":
            if not text.isdigit() or int(text) < 1:
                return await m.reply_text("Please send a whole number ≥ 1, or /cancel.")
            uid, period, limit = state["uid"], state["period"], int(text)
            if uid == 0:
                await access_db.set_default_quota(period, limit)
            else:
                await access_db.set_user_quota(uid, period, limit)
            await access_db.log(uid, "limit_set", m.from_user.id, describe_quota(period, limit))
            inputs.pop(m.from_user.id)
            await m.reply_text(f"✅ Limit saved: <b>{describe_quota(period, limit)}</b>\n/admin", parse_mode=HTML)

        elif kind == "invite_user":
            target = await c.get_users(int(text) if text.lstrip("-").isdigit() else text.lstrip("@"))
            inputs.pop(m.from_user.id)
            token = await access_db.create_invite(m.from_user.id, target.id)
            await access_db.log(target.id, "invited", m.from_user.id)
            link = invite_link(token)
            try:
                await c.send_message(
                    target.id,
                    "📨 You have been invited to use this bot. Tap Accept to get access.",
                    reply_markup=InlineKeyboardMarkup([[B("✅ Accept", callback_data=f"inv:acc:{token}")]]),
                )
                note = f"✅ Invite sent to {who({'id': target.id, 'first_name': target.first_name})}."
            except Exception:
                note = (f"⚠️ Couldn't DM {who({'id': target.id, 'first_name': target.first_name})} "
                        "(they haven't started the bot). Send them this link instead:")
            await m.reply_text(
                f"{note}\n\n<code>{link}</code>", parse_mode=HTML, disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(share_markup(link)),
            )

        elif kind == "group_id":
            chat = await c.get_chat(int(text) if text.lstrip("-").isdigit() else text.lstrip("@"))
            await c.get_chat_member(chat.id, "me")  # raises if the bot isn't in it
            inputs.pop(m.from_user.id)
            await access_db.note_chat(chat, enabled=True)
            await access_db.log(0, "group_added", m.from_user.id, str(chat.id))
            asyncio.get_event_loop().create_task(backfill_join_requests())
            await m.reply_text(f"✅ Added <b>{html.escape(chat.title or str(chat.id))}</b> (<code>{chat.id}</code>).\n/admin", parse_mode=HTML)
    except Exception as e:
        await m.reply_text(f"❌ Couldn't do that: <code>{html.escape(str(e))}</code>\nTry again or /cancel.", parse_mode=HTML)
