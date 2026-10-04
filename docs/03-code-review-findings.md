# Code Review Findings

Review date: 2026-10-03. Scope: whole repository including the new access system (`streamgram/utils/access.py`, `streamgram/bot/plugins/access_admin.py`, changes in `start_help.py` / `stream.py`).
The review itself changed no code. **High (H1–H8), Medium (M1–M14) and Low (L1–L12) items were fixed afterwards; see the status notes.**

How certain is each item?
- **Verified** — reproduced by a script or seen in the live logs / database.
- **Code** — clear from reading the code, not exercised live.
- **Unverified** — suspected, needs a live test.

Severity: **High** = wrong data, security hole or feature not working · **Medium** = real but limited impact · **Low** = polish / hygiene.

> Supersedes the shorter list in `01-architecture-and-flow.md` §13 (items there are repeated below where still valid; fixed ones are noted at the end).

---

## High

### H1. Range requests return wrong/truncated bytes — **Verified** (simulation of `yield_file`)
> ✅ **Fixed 2026-10-03** — Range math rewritten: `part_count` now derived from the chunk-aligned offset, last part always cut (`stream_routes.py`, `custom_dl.yield_file`). Tested byte-exact on 311 ranges incl. random ones.
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

`streamgram/server/stream_routes.py:130` computes `part_count = ceil(req_length / chunk)` from the *request length*, ignoring that the start is rounded **down** to a chunk boundary (`offset`). Combined with `custom_dl.py:209` and `:213` (the last chunk is yielded whole, never cut by `last_part_cut` when `part_count > 1`), results for a 5 MB file:

| Request | Expected | Got |
|---|---|---|
| whole file | 5,000,000 B | 5,000,000 B ✔ |
| `bytes=1000000-` (a video player seeking) | 4,000,000 B | **3,194,304 B** |
| `bytes=1000000-1100000` | 100,001 B | **0 B** |
| `bytes=131071-131072` (straddles a chunk) | 2 B | **0 B** |

Effect: seeking in a video, resuming a download, or download managers using multi-part ranges get truncated/empty bodies while the headers claim the full range. Fix: compute `part_count = ceil((until - offset + 1) / chunk)` and cut the final part with `last_part_cut` in all cases.

### H2. Invalid / unusual Range headers crash with 500 — **Verified** (`chunk_size(0)` → `math domain error`)
> ✅ **Fixed 2026-10-03** — New `parse_range()`: suffix/open/over-long/multi ranges handled, malformed header ignored, unsatisfiable → `416` with `Content-Range: bytes */size`; zero-length files and `chunk_size(0)` handled. Also `Content-Length` is now sent and `Content-Range` only on 206.
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

`stream_routes.py:118` splits `bytes=a-b` blindly: `bytes=-500` (suffix range) → `int('')` error; `bytes=0-0` or a 1-byte file → `req_length == 0` → `math.log2(0)` error; `until >= file_size` is never clamped; multi-range requests are unsupported. Should return `416 Range Not Satisfiable` / clamp.

### H3. Stored XSS through file names — **Code**
> ✅ **Fixed 2026-10-03** — `render_template.py` HTML-escapes file name, heading and src; `dl.html` href is quoted; None-safe for missing name/mime/size.
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

`render_template.py` pastes `file_data.file_name` into `req.html` (`<title>%s</title>`, line 39) and `dl.html` (`<title>`, `<marquee>`) **without HTML escaping**. Anyone who can upload a file named `<script>…</script>` gets script execution for everyone who opens the watch link. Fix: `html.escape()` every inserted value (and quote the `href` in `dl.html:25`).

### H4. Non-media "watch" page fails — **Code**
> ✅ **Fixed 2026-10-03** — Download page uses `FileId.file_size`; the self-HTTP request is gone.
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

`render_template.py:34` reads the `Content-Length` header of the bot's own download URL, but `media_streamer` never sets `Content-Length` (it streams chunked). `int(None)` raises `TypeError` → 500 on every `/watch/...` page for documents/other files. Use `file_data.file_size` (already known) instead of an HTTP self-request.

### H5. Two ways around the access system — **Code**
> ✅ **Fixed 2026-10-03** — Channel posts are only served if a human admin of that channel has bot access (`access.channel_sponsor`, cached 5 min); that admin's limit is applied and usage recorded; otherwise ignored silently (nothing posted in the channel). Join-request self-service is a design choice and unchanged.
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

1. **Channel entry point is ungated.** `stream.py:173 channel_receive_handler` handles media posted in *any* channel where the bot is admin; it has no access check and no usage limit (only `MY_PASS`, which is unset). Anyone who adds the bot as a channel admin gets links outside approval and limits.
2. **Anyone who can request to join an access group gets access.** By design (join-request = default access), but if the group has a public/invite link, the "approval" is effectively self-service. Declined or withdrawn requests also keep access, because `join_requests` entries are only removed on a group ban.

### H6. The access group is not recognised right now — **Verified** (live log)
> ✅ **Fixed 2026-10-03** — `PeerIdInvalid` is now logged loudly (once per 10 min per chat); join-request backfill retries with back-off until it succeeds and is re-triggered when a group is added; `/admin → Groups` shows whether the bot can see each group. **Root cause (bot not in / not seeing the group) still needs the bot added to the group as admin.**
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

Startup logs `Could not backfill join requests for <USER_GROUP_ID>: Peer id invalid` and `access_chats` is empty. The bot's session doesn't know that chat (not a member, or hasn't received any update from it yet). Consequences: membership checks return "not a member" for everyone, and join requests can't be received. `access.py:264` swallows `PeerIdInvalid` **silently**, so nothing in the logs says why a real member was denied, and the backfill is never retried.

### H7. `/login` can't work and answers in groups — **Code**
> ✅ **Fixed 2026-10-03** — `/login` is private-only and no longer needs pyromod (own 90 s password state; the password message is deleted).
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

`stream.py:52` registers `/login` for **any chat** (no `filters.private`), so a user typing `/login` in a group the bot is in (including the access group) makes the bot reply there — contradicting the rule "send nothing to the group". The handler also calls `c.listen(...)` which needs `pyromod`, whose import is commented out in `bot/__init__.py`, so it errors. Currently masked because `MY_PASS` is not set.

### H8. Public web port is flooded by scanners and turns every bad path into a 500 — **Verified** (logs)
> ✅ **Fixed 2026-10-03** — Routes no longer turn `HTTPException` into 500; no-hash requests are rejected before any Telegram call; paths are matched with anchored patterns; errors logged without tracebacks to the client; uvicorn log level set to `error`.
> _Not yet deployed to the running pm2 service until it is restarted; offline-tested only._

`stream_routes.py:76` raises `HTTPException(400)` inside the `try`, whose `except Exception` converts it into a **500**. The pm2 out log contains **134,777** `500` responses (bots probing `/config.yml`, etc.), each printing a traceback. Same pattern in `/watch/...` (`re.search(...).group(1)` on no match → AttributeError → 500).

---

## Medium

| ID | Finding | Where | Certainty |
|---|---|---|---|
| M1 | **Race in `touch()`**: two simultaneous first messages (e.g. an album) from a group-only user both `insert_one`; the unique index on `id` makes the second raise `DuplicateKeyError` inside the middleware, so that message is silently dropped. | `access.py:127` | Code |
| M2 | **Any DB error in the middleware silently drops the message.** Pyrogram logs the exception and stops dispatch; the user gets no reply. Same for `check_quota` in `stream.py`. Fail-closed is right, but the user should get "try again later". | `start_help.check_user`, `stream.py` | Code |
| M3 | **Limits can be bypassed by bursts**: quota is checked before and recorded after sending, so several files sent at once all pass. | `access.py:203`, `stream.py` | Code |
| M4 | **Request spam**: a rejected/revoked user can tap *Request Access* again and again, each time messaging every owner. No cooldown. | `access_admin.py:131` | Code |
| M5 | **Revoke can be a no-op**: a user who is also a member of an access group keeps access after "Revoke" (only Ban overrides groups). Detail page hides Revoke only for `group`-status users. | `has_access` | Code |
| M6 | **Invite by numeric ID only works if the bot already knows the user** (`get_users` → `PeerIdInvalid` otherwise). The "Create link" route works for everyone. | `access_admin.py` owner_input | Code |
| M7 | **Join-request backfill runs once at startup** and is never retried after the bot learns the group; pending requests made before that are missed. | `access_admin.py:75` | Code |
| M8 | **Empty inline keyboard**: after Approve/Reject on a request notification the code edits the message with `InlineKeyboardMarkup([])`. Should remove the buttons, but not tested against Telegram. | `access_admin.py:229` | Unverified |
| M9 | **`/start <id>` deep link** (`start_help.py:160`) lets any allowed user read file names/sizes of arbitrary messages in `BIN_CHANNEL` by guessing ids, builds a link **without the required hash** (so the link 403s), and `int(usr_cmd)` crashes on non-numeric text (`/start hello`). | `start_help.py` | Code |
| M10 | **Weak link secret**: only the first 6 characters of Telegram's `file_unique_id`; links never expire or can be revoked. | `file_properties.get_hash` | Code |
| M11 | **`TRUSTED_USERS` parsing is fragile**: `.split(" ")` — a double space, trailing space or unset value gives `int('')` and the bot won't start. (Unset was already listed; a stray space is equally fatal.) | `vars.py:53` | Code |
| M12 | **`/broadcast` without a replied-to message** forwards `None` to every user and logs a failure for each; banned users still receive broadcasts; users removed from `users` are not removed from `access_users`. | `admin.py:50` | Code |
| M13 | **`FloodWait.x`** no longer exists in Pyrogram 2.x (`.value`), and `broadcast_helper.send_msg` returns an un-awaited coroutine after sleeping. | `broadcast_helper.py`, `stream.py` | Code |
| M14 | **Self-HTTP request** to the public URL for non-media pages needs the server to reach its own `FQDN` (fails behind NAT/HTTPS) and opens a real Telegram download just to read a header. (Goes away with H4's fix.) | `render_template.py` | Code |

## Medium — status (fixed 2026-10-03)

| ID | Result |
|---|---|
| M1 | **Corrected finding:** inside one process the 60 s throttle already prevented the race, so it was not reachable as described. `touch()` now also falls back to an update on `DuplicateKeyError` (covers restarts / a second process). |
| M2 | Fixed. A database failure in the access check or the limit check now answers "Something went wrong on my side, try again"; a failure while recording activity is logged and never blocks the user. |
| M3 | Fixed. `AccessDB.reserve()` checks the limit and counts the link under a per-user lock; `refund()` gives it back if no link was delivered. 10 simultaneous files with a limit of 3 → exactly 3 allowed. Also used for channel posts. |
| M4 | Fixed. A rejected or revoked person must wait 1 hour (`REQUEST_COOLDOWN`) before asking again. |
| M5 | Fixed. Revoke/Reject now warn the owner when the person still has access through an access group or join request (use Ban); the user page shows the same warning. |
| M6 | Fixed. Inviting a numeric ID the bot has never seen creates a link that only that account can redeem, instead of an error. Unknown @usernames still cannot be resolved. |
| M7 | Fixed earlier with H6 (retrying backfill, re-triggered when a group is added). |
| M8 | Fixed. Decision buttons are replaced by one inert outcome button (`adm:noop`) instead of an empty keyboard. |
| M9 | Fixed. `/start <message id>_<hash>` needs a valid hash (so ids cannot be enumerated), shows an escaped name and working links, honours revocation, and rejects anything else with one generic message. The old call used a keyword that does not exist in this Pyrogram version and could never have worked. |
| M10 | Partly fixed. New links carry 12 characters of the file id (links created earlier keep working with 6). Owners can stop serving a file with `/revoke <message id>` and undo it with `/unrevoke <message id>` (checked on every request, cached 30 s). **Not done:** automatic link expiry. |
| M11 | Fixed. `TRUSTED_USERS` and `OWNER_ID` accept spaces and/or commas; blank or unset means nobody. |
| M12 | Fixed. `/broadcast` without a replied-to message explains how to use it; banned people are skipped and counted. Records in `access_users` are intentionally kept when someone leaves the broadcast list, because access does not depend on it. |
| M13 | Fixed. `FloodWait.value` is used everywhere and the broadcast retry is awaited; a rate-limited file upload is refunded and the user is asked to resend. |
| M14 | Fixed earlier with H4 (no self-request). |

Regression tests: `tests/test_medium_fixes.py` (run together with `tests/test_high_fixes.py` via `pytest tests`). Checked live on the running service: legacy 6-character links still stream, `/revoke`-style revocation returns 404 for both the file and the watch page, and restoring it returns 206.

## Low / hygiene

| ID | Finding |
|---|---|
| L1 | **Unbounded log growth**: `logs/` has **51,793** files (306 MB) — a new `log_*.log` per start and `RotatingFileHandler` without `maxBytes`; `request_logs.csv` is **59 MB** and written with blocking I/O inside async middleware; pm2 `out` log is 60 MB. No rotation anywhere. |
| L2 | `Var.UPDATES_CHANNEL` defaults to the string `"None"`; `start_help.py` tests `is not None` (always true) so `/start`, `/help`, `/about` take the force-subscribe branch and fall into the generic "Welcome" text when it is unset. `stream.py` compares correctly. |
| L3 | `bool(getenv('NO_PORT'/'HAS_SSL', False))` treats `"false"`/`"0"` as true. `WORKERS` assigned twice. |
| L4 | Four separate `Database` objects (+ the new `AccessDB`) each create a Mongo client; each plugin repeats the init boilerplate and may `sys.exit(1)`. `users` collection has no unique index on `id`. |
| L5 | `MY_PASS` is stored in clear text per user in Mongo (`ag_p`). |
| L6 | `stats` is available to every allowed user (leaks server CPU/RAM/disk/network); README calls it `status`. |
| L7 | `yield_file` swallows `TimeoutError` silently (truncated streams; see `unknown_errors.txt`), and always fetches one extra chunk after the last part. `Content-Range` is sent on non-range 200 responses. |
| L8 | `process.json` hard-codes a Windows Anaconda path; the live service is managed by pm2 outside the repo. `requirements.txt` still lacks `fastapi`/`uvicorn` (they happen to be installed here). |
| L9 | `edit()` in `access_admin` assumes `cq.message` exists (old/inaccessible messages raise). History rows for group events show uid `0`. |
| L10 | Duplicate helpers (`readable_time`/`get_readable_time`, `file_size.py` unused), `utils_bot.py` outside the package, placeholder `setup.cfg`/`sonar-project.properties`, upstream branding and donation links, untracked scratch files `solution_distances*.py`. |
| L11 | `req.html` is rendered with `.replace('tag', tag)` — it only works because "tag" appears once in that template. |
| L12 | `access_admin.py` and `utils/access.py` (new) are untracked and uncommitted; `docs/` too. |

---

## Low — status (fixed 2026-10-04)

| ID | Result |
|---|---|
| L1 | Fixed. Fixed-name rotating `info.log` / `error.log` with size limits replace one file per start; level from `LOG_LEVEL` (INFO); old run logs older than a week deleted; pm2 logs trimmed; the request CSV is kept, no longer written on the event loop, and rotates monthly into `request_logs_YYYY-MM.csv` (nothing deleted). |
| L2 | Fixed. `UPDATES_CHANNEL` is `None` when unset / blank / "none"; all checks use truthiness. |
| L3 | Fixed. `HAS_SSL`, `NO_PORT` accept true/false/1/0/yes/no/on/off; duplicate `WORKERS` removed. |
| L4 | Fixed. One shared Mongo client and one `Database` per name (`Database.shared`); unique index on `users.id` (8 duplicate rows removed first). |
| L5 | Fixed. Login stores a SHA-256 token of `MY_PASS`, never the password (changing `MY_PASS` logs everyone out). |
| L6 | Fixed. `/stats` (also `/status`) is owner-only. |
| L7 | Fixed earlier with H1 (timeouts logged, no extra chunk, `Content-Range` only on 206). |
| L8 | Fixed. `fastapi` and `uvicorn` added, unused `pyromod` removed, `requirements-dev.txt` added, `process.json` made portable (not applied to the running service). |
| L9 | Fixed. Stale menus answer "send /admin again"; history shows "(system)" instead of uid 0. |
| L10 | Mostly fixed. Formatting helpers moved into the package without duplicates, unused `file_size.py`, `setup.cfg`, `sonar-project.properties` removed. **Left for later:** upstream branding / donation / channel links in the texts. The untracked `solution_distances*.py` files were not touched. |
| L11 | Fixed. The media template uses an unambiguous `__MEDIA__` placeholder. |
| L12 | Work is committed locally (not pushed). |

## Behaviour notes (not bugs, but easy to misread)

- Owners and `TRUSTED_USERS` skip access checks and limits, so test with a different account (this explained the "got access without approval" test on 2026-10-03).
- `month` limits are a rolling 30 days; all windows are rolling.
- Join-request users appear in *Users with access* only after their first message to the bot (status `group`).

## Already fixed earlier (kept for history)

- Join-request users got "Access Denied" (now allowed).
- Left/banned group members counted as members because `get_chat_member` returned without error (now checked by status).
- Owners had to be duplicated in `TRUSTED_USERS` (now exempt).
- `USER_GROUP_ID` default `1` caused useless lookups (now only negative chat ids count).
- `channel_receive_handler` would have replied in/edited posts of an access channel (now skipped).

## Suggested fix order

1. H6 (get the bot to recognise the group; log `PeerIdInvalid`) — blocks group-based access.
2. H1 + H2 (range math) — user-visible data corruption.
3. H8 + L1 (stop 500s/log flood, add rotation) — protects the server.
4. H3 + H4 (escape HTML, drop the self-request).
5. H5 + H7 (close the channel/`/login` gaps).
6. M1–M4 (access-system robustness), then the rest.
