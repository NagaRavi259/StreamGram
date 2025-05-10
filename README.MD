<h1 align="center"><b>⚡ StreamGram</b></h1>

<p align="center">
  <b>Turn any Telegram file into a permanent streaming or download link.</b>
  <br />
  Send the bot a file, get back a fast, resumable download link and a
  watch-in-browser link — nothing is ever stored on the server; it's streamed
  straight from Telegram on demand, chunk by chunk.
</p>

<hr>

## About

StreamGram forwards whatever you send it into a private Telegram "bin"
channel, then serves the bytes back over plain HTTP with full `Range`
support, so video/audio players can seek and downloads can resume. On top of
that it adds access control, link security, automatic link expiry, and a
fix for the one thing browsers still can't play natively — files whose audio
track is AC3/E-AC3/DTS/TrueHD.

## Features

- Permanent **download** and **watch/stream** links for any file you send
- Range-aware streaming — seeking and resumable downloads just work
- Access control: approve/reject/ban users, invite links, per-group access,
  and configurable usage limits
- Configurable link expiry (default unlimited, per-user override)
- Per-link security tokens instead of a guessable hash
- Optional password-only login path, independent of admin approval
- Automatic audio fix for AC3/E-AC3/DTS/TrueHD tracks — re-encodes just the
  audio to AAC on the fly, video is never touched
- Structured logging (rotating info/error logs, CSV request log)
- Multi-bot load balancing (`MULTI_TOKEN_*`) to spread Telegram's flood
  limits across several bot accounts
- MongoDB-backed users, broadcast, and system stats for the owner

See [`docs/01-architecture-and-flow.md`](docs/01-architecture-and-flow.md) for
the full architecture and request flow.

## Commands

```
start     - check the bot is alive
status    - bot status (owner only)
help      - help section
about     - about this bot
users     - total user count (owner only)
broadcast - broadcast a message to all users (owner only)
login     - log in, if password access is enabled
```

## Running your own

### Deploy on Heroku

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/NagaRavi259/StreamGram)

### Run locally or on a VPS

```bash
git clone https://github.com/NagaRavi259/StreamGram
cd StreamGram
python3 -m venv venv
. ./venv/bin/activate
pip install -r requirements.txt
cp config.env.example config.env   # then fill it in, see below
python3 -m streamgram
```

Stop the bot with <kbd>CTRL</kbd>+<kbd>C</kbd>.

For Docker, systemd or PM2, see
[`docs/04-deployment.md`](docs/04-deployment.md).

### Configuration

Copy `config.env.example` to `config.env` and fill it in (or set the same
names as real environment variables, e.g. on Heroku). `config.env` is
git-ignored — never commit real values.

<details>
  <summary><b>Core variables</b></summary>

| Variable | Required | Purpose |
|---|---|---|
| `API_ID`, `API_HASH` | yes | From [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | yes | From [@BotFather](https://telegram.dog/BotFather) |
| `BIN_CHANNEL` | yes | Private channel the bot uses as file storage; add the bot as admin |
| `OWNER_ID` | recommended | Your Telegram user id(s), space/comma separated — admin commands |
| `DATABASE_URL` / `MONGO_*` | yes | MongoDB connection (plain `mongodb` or `mongodb+srv`) |
| `PORT`, `WEB_SERVER_BIND_ADDRESS` | no | Web server bind, default `8080` / `0.0.0.0` |
| `FQDN`, `HAS_SSL`, `NO_PORT` | no | How the public link URL is built |

</details>

<details>
  <summary><b>Access control</b></summary>

| Variable | Purpose |
|---|---|
| `USER_GROUP_ID` | Chat whose members (and join-requesters) may use the bot |
| `TRUSTED_USERS` | Ids that always have access, space/comma separated |
| `UPDATES_CHANNEL` | Optional force-subscribe channel |
| `MY_PASS` | Enables `/login` password-gated access (off unless set) |

</details>

<details>
  <summary><b>Audio fix, multi-bot, logging</b></summary>

| Variable | Purpose |
|---|---|
| `ENABLE_AUDIO_FIX` / `MAX_CONCURRENT_AUDIO_FIX` | AC3/E-AC3/DTS/TrueHD → AAC fix; needs `ffmpeg`/`ffprobe` on `PATH` |
| `MULTI_TOKEN_1`, `MULTI_TOKEN_2`, ... | Extra bot tokens to share the streaming load |
| `LOG_LEVEL`, `LOG_DIR`, `LOG_MAX_MB`, `LOG_BACKUPS` | Logging verbosity, location and rotation |

</details>

See [`config.env.example`](config.env.example) for the complete, documented
list.

### How to use

:warning: Add the bot to `BIN_CHANNEL` as an admin before using it.

Forward or send any file to the bot in a private chat and it replies with
name, size, and both a watch and a download link. It also works in channels:
add the bot as a channel admin and new posts get Watch/Download buttons
automatically.

## License

Licensed under the [GNU AGPLv3](LICENSE). This project began from an
AGPL-licensed open-source Telegram file-to-link bot and has since been
substantially rewritten: access control, link security and expiry,
structured logging, the audio fix, and more.
