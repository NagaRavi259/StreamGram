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
- [ ] **A1.** Add `fastapi` and `uvicorn` to `requirements.txt`; verify a fresh venv install boots
- [ ] **A2.** Make `TRUSTED_USERS` tolerate an empty/unset value (currently `int('')` crashes `vars.py`)
- [ ] **A3.** Decide `USER_GROUP_ID` default (currently `1`, which denies everyone if unset); exempt `OWNER_ID` from the middleware
- [ ] **A4.** Parse boolean env vars properly (`NO_PORT`, `HAS_SSL`); remove duplicate `WORKERS`
- [ ] **A5.** Fix `UPDATES_CHANNEL` default (`"None"` string vs `None`) in `start_help.py`
- [ ] **A6.** Add a sanitized `config.env.example` and document every variable
- [ ] **A7.** Replace Windows-specific path in `process.json` (or document it as local-only)
- [ ] **A8.** Ensure `logs/` is created before logging setup on a fresh clone

### B. Features / bot behaviour
- [ ] **B1.** Decide on `/login` + `MY_PASS`: re-enable `pyromod` (or reimplement with a conversation/state dict) or remove the feature
- [ ] **B2.** Restrict `/stats` to owners; align README command list (`status` vs `stats`)
- [ ] **B3.** Fix broadcast FloodWait handling (`await`, `e.value`) and in `stream.py`
- [ ] **B4.** Factor the repeated force-subscribe/ban block out of `start`, `help`, `about`, and file handler into one helper
- [ ] **B5.** Improve the access-denied message (how to get access / contact)
- [ ] **B6.** Replace original-author branding, donation links and channel links with project-owned ones

### C. Streaming server
- [ ] **C1.** Investigate recurring `503 Timedout upload.GetFile` (see `unknown_errors.txt`); stop silently swallowing `TimeoutError` in `yield_file`
- [ ] **C2.** Stronger link tokens / optional expiry (6-char hash is weak)
- [ ] **C3.** Only send `Content-Range` on 206 responses
- [ ] **C4.** Avoid the self-HTTP request in `render_page` for non-media files (use size from `FileId`)
- [ ] **C5.** Async/buffered request logging; rotate or cap `request_logs.csv`

### D. Database
- [ ] **D1.** Single shared `Database` instance instead of four
- [ ] **D2.** Unique index on `users.id`; avoid duplicate inserts from `add_user_pass`
- [ ] **D3.** Do not store the plain `MY_PASS` per user (store a flag/token instead)

### E. Quality & operations
- [ ] **E1.** Add unit tests (helpers: `chunk_size`, range math, `get_hash`, URI builder)
- [ ] **E2.** Fix log rotation (`maxBytes` + `backupCount`) and clean existing `logs/`
- [ ] **E3.** Remove dead code (`file_size.py`, duplicate `readable_time`, placeholder `setup.cfg`/`sonar-project.properties`) or make them real
- [ ] **E4.** Move `utils_bot.py` into the package
- [ ] **E5.** Dockerfile / systemd or PM2 instructions for the VPS deployment
- [ ] **E6.** Move or delete `solution_distances*.py` (unrelated to the bot; currently untracked)

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
- [ ] **F10.** Add the bot to the access group as admin so group membership checks work

## 6. Open questions for the owner

1. Which environment is the real deployment — this Linux box, a Windows PC (PM2 path), or Heroku/Railway?
2. Should `OWNER_ID` users bypass the group-membership check automatically?
3. Is password login (`MY_PASS`) still wanted now that group-based authorization exists?
4. Should links stay permanent, or do you want expiry/revocation?
5. Is the upstream branding meant to stay?

## 7. Log

| Date | What happened |
|---|---|
| 2026-10-03 | Built access system (`utils/access.py`, `plugins/access_admin.py`, middleware + `stream.py` limits). |
| 2026-10-04 | Monthly rotation of the request CSV (`utils/request_log.py`), old history split into monthly files, test rows removed from the live file, tests isolated from the real logs. |
| 2026-10-04 | Added automatic link expiry (module `link_expiry.py`, `/admin` → Link expiry, 410 responses); new logging setup and cleanup of 51,786 old log files; fixed Low findings L1–L12; tests in `tests/test_expiry_logging_low.py`. |
| 2026-10-03 | Fixed all Medium findings M1–M14 (atomic limits, deep-link hash, revoke command, longer hashes, cooldown, error replies); tests in `tests/test_medium_fixes.py`. |
| 2026-10-03 | Fixed all High findings H1–H8 (range math, 416/400/403 handling, HTML escaping, channel gate, /login, group diagnostics). |
| 2026-10-03 | Full code review; findings documented in `docs/03-code-review-findings.md` (no code changed). |
| 2026-10-03 | Added join-request access to middleware (`start_help.py`, `database.py`); docs updated. |
| 2026-10-03 | Read the full codebase; created `docs/01-architecture-and-flow.md` and this progress tracker. No bot code changed. |
| _(add rows as work proceeds)_ | |
