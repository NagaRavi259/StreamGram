# File-To-Link Bot — Progress & Checklist

Living document. Update the checkboxes and the "Log" section as work happens.
Last updated: 2026-10-03 (initial version, derived from git history, working tree and a full code read — see `01-architecture-and-flow.md`).

Legend: `[x]` done · `[~]` in progress / partly done · `[ ]` not started · items marked **(proposed)** were suggested by the code review, not yet agreed.

---

## 1. Where we are now

The bot works end to end: file in → forwarded to bin channel → permanent stream/download links → streamed from Telegram with range support, optionally balanced across several helper bots. Recent work focused on **access control** (only trusted users or members of a specific Telegram group may use the bot) and **MongoDB connectivity** (SRV URIs, startup ping).

Working tree: clean for tracked files; two untracked files (`solution_distances.py`, `solution_distances_v2.py`) that are unrelated to the bot.

## 2. Progressed (done)

Taken from `git log` and the code:

- [x] Base bot forked from filestreambot-pro (private + channel file handlers, FastAPI streaming server, multi-client support)
- [x] Range-aware streaming with per-DC media sessions and load-balanced clients
- [x] Watch page for video/audio, download page for other files
- [x] Branding / texts customised (`Update start_help.py`, `stream.py`, README commits)
- [x] MongoDB user store via Motor, owner `/users` and `/broadcast`
- [x] `/stats` system-status command
- [x] Request logging to `logs/request_logs.csv`; timestamped run logs
- [x] Config moved to `config.env` via `python-dotenv`; secrets and sessions git-ignored
- [x] "Added all local changes to git" baseline commit (`6981424`)
- [x] **Middleware for user authorization** (`USER_GROUP_ID`, `TRUSTED_USERS`) and **`mongodb+srv` URI support** (`529a82f`)
- [x] "Code refactor" (`94a2e47`)
- [x] Project documentation created (`docs/`)
- [x] Access system: Request Access button + owner approve/reject/ban, `/admin` menu (invite by username/link, users newest-first, groups, history, ban/revoke), per-user/default link limits — **uncommitted; logic smoke-tested offline, not yet tried live on Telegram**
- [x] Join-request access: users who requested to join `USER_GROUP_ID` get access by default (`join_requests` collection, handler + startup backfill); left/banned users no longer pass the membership check — **uncommitted, untested against live Telegram**

## 3. In progress (currently working on)

> Fill this in each session. Nothing is recorded as actively in-flight in git right now.

- [~] Understanding / documenting the existing codebase (this `docs/` folder)
- [~] Access-control rollout: middleware is implemented, but its config edge cases are not hardened yet (see §5, items A1–A3)

## 4. Next up / planned

**(proposed)** — confirm priorities before starting.

- [ ] Make a clean install work (dependencies, startup with minimal config)
- [ ] Harden access control and config parsing
- [ ] Fix known functional bugs (login, broadcast, updates-channel check)
- [ ] Reduce duplication (DB instances, repeated force-subscribe blocks)
- [ ] Add tests and basic CI
- [ ] Decide what to do with unrelated scratch files in the repo root

## 5. Detailed checklist

### A. Setup & configuration
- [x] **A1.** `fastapi`/`uvicorn` are in `requirements.txt`
- [x] **A2.** `TRUSTED_USERS` tolerates empty/unset/comma-or-space-separated (`vars.py`)
- [x] **A3.** `OWNER_ID` is exempt from the middleware; `USER_GROUP_ID` default `1` is now harmless (`access.group_ids()` only counts negative ids, so an unset/positive value just means "no group configured" instead of denying everyone)
- [x] **A4.** `NO_PORT`/`HAS_SSL` parsed with `_env_bool` (true/false/1/0/yes/no/on/off); no duplicate `WORKERS`
- [x] **A5.** `UPDATES_CHANNEL` is `None` when unset/blank/"none" (`_env_text`)
- [x] **A6.** `config.env.example` now documents every variable `vars.py` reads (`BANNED_CHANNELS`, `SESSION_NAME`, `WORKERS`, `SLEEP_THRESHOLD`, `PING_INTERVAL`, `APP_NAME` added 2026-10-05)
- [x] **A7.** `process.json` now just runs `python3 start_bot.py` — no Windows path
- [x] **A8.** `logging_config.py` does `os.makedirs(log_dir, exist_ok=True)` before use

### B. Features / bot behaviour
- [x] **B1.** `/login` + `MY_PASS` kept; reimplemented without `pyromod`; standalone access path independent of admin approval (F13). Disabled by default.
- [x] **B2.** `/stats` (and `/status`) owner-only
- [x] **B3.** Broadcast FloodWait uses `.value` and is awaited (`broadcast_helper.py`, `stream.py`)
- [x] **B4.** Force-subscribe/ban block factored into `streamgram.utils.force_subscribe.enforce_updates_channel`, used by `start`/`help`/`about` and `private_receive_handler` (was duplicated 5x; `start_help.py` alone dropped from 317 to 198 lines)
- [x] **B5.** Access-denied message has the Request Access button and (when `MY_PASS` is set) a `/login` hint
- [x] Pending decision: **B6** branding/donation/channel links kept as-is for now (§6) — later fully rebranded

### C. Streaming server
- [x] **C1.** `yield_file`'s `except (TimeoutError, AttributeError)` now logs a warning instead of silently passing; the underlying intermittent Telegram DC timeout itself isn't something our code can fix
- [x] **C2.** New links now carry a random, unguessable token (`streamgram.utils.link_security.LinkTokens`, `secrets.token_urlsafe`, ~72 bits), not a prefix of `file_unique_id`; validated in `stream_routes.py`, `render_template.py` and the `/start <id>_<hash>` deep link. Links issued before this (no token on record) still validate the old way, so nothing already handed out breaks.
- [x] **C3.** `Content-Range` only sent on 206
- [x] **C4.** Self-HTTP request removed; uses `FileId.file_size`
- [x] **C5.** Request logging moved off the event loop (worker thread) and rotates monthly

### D. Database
- [x] **D1.** `Database.shared(name)` — one client/instance per database name, used everywhere
- [x] **D2.** Unique index on `users.id` exists; duplicate-insert races are handled (touch() falls back to update on `DuplicateKeyError`)
- [x] **D3.** `MY_PASS` stored as a SHA-256 token, never the plain password

### E. Quality & operations
- [x] **E1.** Test suite now covers range math, hashing, formatting helpers, multi-client startup, keep-alive, branding handlers and the full admin menu (`tests/`, 5 files) — added 2026-10-05
- [x] **E2.** Log rotation fixed (`LOG_MAX_MB`/`LOG_BACKUPS`); old logs cleaned
- [x] **E3.** `setup.cfg`/`sonar-project.properties` removed, `file_size.py` gone, and `streamgram/utils/formatting.py` (the duplicate `get_readable_file_size`/`readable_time`) deleted — `/stats` now reuses `human_readable.humanbytes` and `time_format.get_readable_time` like the rest of the app
- [x] **E4.** No `utils_bot.py` outside the package anymore (removed/merged)
- [x] **E5.** `Dockerfile` + `.dockerignore` added; `docs/04-deployment.md` covers Docker, systemd and PM2 (couldn't run `docker build` here — no Docker daemon available — so the image itself is untested, only reviewed)
- [x] **E6.** No `solution_distances*.py` scratch files present

### Found and fixed 2026-10-05 (not in the original review)
- [x] **N1.** `humanbytes`: fixed the off-by-one-unit bug on exact multiples of 1024 (loop now uses `>=` and is bounded, so it also no longer `KeyError`s past `Ti`).
- [x] **N2.** `initialize_clients()`: a failed `MULTI_TOKEN_n` client is now skipped (`dict(c for c in clients if c is not None)`) instead of crashing the whole startup.

### F. Findings from the 2026-10-03 code review (details in `03-code-review-findings.md`) — F1–F5 fixed in code 2026-10-03, offline-tested, **not yet restarted/committed**
- [x] **F1.** H6 — make the bot recognise the access group; log `PeerIdInvalid`; retry the join-request backfill
- [x] **F2.** H1/H2 — fix range math (`part_count`, last-part cut) and invalid/suffix ranges (416)
- [x] **F3.** H8/L1 — return 400/404 instead of 500 for bad paths; add log rotation (run logs, `request_logs.csv`, pm2)
- [x] **F4.** H3/H4 — HTML-escape file names in templates; use `FileId.file_size` instead of the self-request
- [x] **F5.** H5/H7 — gate/limit the channel handler; make `/login` private-only (or remove it)
- [x] **F6.** M1–M4 — handle `touch()` duplicate-key race, friendly error on DB failure, burst-proof limits, request cooldown
- [x] **F7.** Access system + high fixes committed and pushed (`4468ad1`)
- [x] **F8.** Medium findings M1–M14 fixed, tested offline and checked live (not yet committed)
- [x] **F9.** Automatic link expiry (default unlimited, per-user override, admin menu) and Low findings L1–L12 (not yet pushed)
- [x] **F11.** Logging reorganised (info.log / error.log, `LOG_LEVEL=INFO`), old logs cleaned, duplicate users removed, unique index added
- [x] **F12.** Request CSV rotates monthly, history preserved
- [x] **F10.** Add the bot to the access group as admin so group membership checks work — done directly
- [x] **F13.** `/login` + `MY_PASS` made a standalone access path: a logged-in user bypasses the admin-approval/access-group system entirely (a ban still wins); kept **disabled by default** (`MY_PASS` unset), turned on when needed. `tests/test_medium_fixes.py` covers bypass, ban-wins, and that `/login` + the password reply are reachable before approval.

## 6. Open questions — answered 2026-10-05

1. Deployment target: run anywhere Python runs; no single fixed target. **Decision kept.**
2. `OWNER_ID` bypass: already exempt from the group-membership check and from usage limits.
3. Password login: **keep it**, as a standalone way in that doesn't need admin approval — not a second gate on top of it. **Left disabled** (`MY_PASS` unset) until turned on. Implemented in F13.
4. Links: **keep expiry configurable** (default unlimited, per-user override) — managed from `/admin` → Link expiry as needed.
5. Branding/donation/channel links: **keep as-is** for now; to revisit later.

(M6 — inviting an unknown numeric ID by `/admin` — explicitly skipped, not being fixed.)

## 7. Log

| Date | What happened |
|---|---|
| 2026-10-05 | Added logging across the entire codebase (every module, class and function in `streamgram/`): DEBUG traces for pure helpers and cache hits/misses, INFO for business events (link created, status changes, admin actions, broadcasts, new users), WARNING for recoverable/unexpected conditions, ERROR/`exception` for failures. Replaced two spots that bypassed logging (`traceback.print_exc()` in `keepalive.py`, a bare `logger.error(str(e))` losing the traceback in `stream.py`). Verified no secrets (passwords, tokens, Mongo URI credentials) are ever logged, and confirmed with a live run that `LOG_LEVEL=INFO` stays clean/high-signal while `LOG_LEVEL=DEBUG` reveals the detailed traces. Full test suite still passes. |
| 2026-10-05 | Fixed everything still open from the checklist: B4 (force-subscribe helper, `utils/force_subscribe.py`), E5 (Dockerfile + `docs/04-deployment.md`), E3 (deleted `utils/formatting.py`, consolidated onto `humanbytes`/`get_readable_time`), A6 (`config.env.example` now lists every variable), C2 (`utils/link_security.py` — real random per-link tokens, legacy links still fall back to the old hash), N1 (`humanbytes` off-by-one-unit), N2 (`initialize_clients()` no longer crashes on one bad `MULTI_TOKEN_n`). M6 explicitly skipped. Tests added for all of the above in `tests/test_full_coverage.py` and `tests/test_expiry_logging_low.py`; full suite (5 files) passes. |
| 2026-10-05 | Answered the open questions (§6). Made `/login` + `MY_PASS` a standalone access path independent of admin approval (a ban still wins); removed the now-redundant password gate from `private_receive_handler` and the non-functional one from `channel_receive_handler`; `check_user` now lets `/login` and the password reply through before approval. Feature stays off by default. F10 and M6 resolved as decided there (F10 done; M6 explicitly skipped). |
| 2026-10-03 | Built access system (`utils/access.py`, `plugins/access_admin.py`, middleware + `stream.py` limits). |
| 2026-10-04 | Monthly rotation of the request CSV (`utils/request_log.py`), old history split into monthly files, test rows removed from the live file, tests isolated from the real logs. |
| 2026-10-04 | Added automatic link expiry (module `link_expiry.py`, `/admin` → Link expiry, 410 responses); new logging setup and cleanup of 51,786 old log files; fixed Low findings L1–L12; tests in `tests/test_expiry_logging_low.py`. |
| 2026-10-03 | Fixed all Medium findings M1–M14 (atomic limits, deep-link hash, revoke command, longer hashes, cooldown, error replies); tests in `tests/test_medium_fixes.py`. |
| 2026-10-03 | Fixed all High findings H1–H8 (range math, 416/400/403 handling, HTML escaping, channel gate, /login, group diagnostics). |
| 2026-10-03 | Full code review; findings documented in `docs/03-code-review-findings.md` (no code changed). |
| 2026-10-03 | Added join-request access to middleware (`start_help.py`, `database.py`); docs updated. |
| 2026-10-03 | Read the full codebase; created `docs/01-architecture-and-flow.md` and this progress tracker. No bot code changed. |
| _(add rows as work proceeds)_ | |
