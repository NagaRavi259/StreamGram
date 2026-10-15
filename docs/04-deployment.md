# Deployment (E5)

Two supported ways to run this in production: a container (Docker), or a plain process
manager on a VPS (systemd or PM2). Both assume a reachable MongoDB and a `config.env`
(or real environment variables) filled in from `config.env.example`.

---

## Option A: Docker

A `Dockerfile` is included at the repo root. It installs `requirements.txt`, copies the
app, creates `logs/`, and runs `python -m streamgram` on port 8080. It does **not** bake in
`config.env`, `*.session` files or `logs/` — those are runtime state, supplied at `docker run`
time.

```bash
docker build -t streamgram .

docker run -d \
  --name streamgram \
  --restart unless-stopped \
  -p 8080:8080 \
  --env-file config.env \
  -v streamgram-logs:/app/logs \
  -v streamgram-sessions:/app/sessions \
  streamgram
```

Notes:
- `--env-file config.env` passes every variable from `config.env.example` as real environment
  variables; `python-dotenv` only loads `config.env` as a fallback, so either form works.
- The sessions volume keeps `*.session` files (the bot's Telegram login) across container
  restarts — without it, every restart re-authenticates and may hit login rate limits. It's
  scoped to `/app/sessions` specifically (not the whole `/app`) so that rebuilding the image
  with code changes still takes effect on the next `docker run` — a volume covering the whole
  app directory would freeze it at whatever was there when the volume was first created.
- `docker logs streamgram` shows the startup banner; `logs/info.log` and `logs/error.log`
  live in the mounted `logs` volume (see `docs/01-architecture-and-flow.md` §15).

To update: rebuild the image and `docker stop/rm` + re-`run`.

### A1: docker compose (bot + MongoDB together)

`docker-compose.yml` at the repo root runs the bot alongside its own MongoDB container —
useful for local development/testing, or a small deployment that doesn't already have Mongo
elsewhere. It does **not** replace a managed/hosted Mongo (Atlas, a shared VPS instance) for a
real deployment with other consumers of that data; use a bare `docker run` with
`MONGO_SCHEMA=mongodb+srv` and the external host for that instead.

```bash
cp config.env.example config.env   # fill in API_ID/API_HASH/BOT_TOKEN/BIN_CHANNEL/OWNER_ID
                                    # at minimum; leave MONGO_HOST as-is, compose overrides it
docker compose up -d --build
docker compose logs -f bot
```

Notes:
- `MONGO_HOST`/`MONGO_PORT` are overridden to point at the `mongo` service regardless of
  what `config.env` says, so the same file still works unmodified with a bare `docker run`
  against an external Mongo.
- Mongo's data lives in the `mongo-data` named volume, persisted across restarts; the bot's
  logs and `*.session` files are in `bot-logs` / `bot-sessions` the same way as Option A.
- `PORT` in `config.env` controls the host-side port too (`${PORT:-8080}:8080`); defaults to
  8080 if unset.
- `docker compose down` stops both containers and keeps the volumes; add `-v` to also wipe
  Mongo's data and the bot's session (next start re-logs-in to Telegram).
- To update: `docker compose up -d --build` again (only rebuilds what changed).

---

## Option B: VPS with a process manager

Either systemd (recommended on a VPS you control) or PM2 (the `process.json` already in the
repo) keep the bot running and restart it on crash or reboot.

### B1: systemd

```ini
# /etc/systemd/system/streamgram.service
[Unit]
Description=StreamGram Telegram bot
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/streamgram
EnvironmentFile=/opt/streamgram/config.env
ExecStart=/opt/streamgram/venv/bin/python -m streamgram
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Setup:
```bash
cd /opt/streamgram
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.env.example config.env   # then fill it in
sudo systemctl daemon-reload
sudo systemctl enable --now streamgram
sudo systemctl status streamgram
journalctl -u streamgram -f      # follow logs (the app's own logs/ still apply too)
```

### B2: PM2

`process.json` is already in the repo root and just runs `python3 start_bot.py`:

```bash
cd /opt/streamgram
python3 -m venv venv  # optional; or install deps globally
venv/bin/pip install -r requirements.txt  # if using a venv, also set "interpreter" in
                                           # process.json to venv/bin/python3
cp config.env.example config.env          # then fill it in
pm2 start process.json
pm2 save                                   # persist across reboots
pm2 startup                                # generates the OS boot hook (follow its printed command)
pm2 logs StreamGram
```

To update: `git pull`, `venv/bin/pip install -r requirements.txt` if dependencies changed,
then `pm2 restart StreamGram` (or `sudo systemctl restart streamgram` for systemd).

---

## Either way

- MongoDB must be reachable from wherever the bot runs; `MONGO_SCHEMA=mongodb+srv` works for
  a hosted cluster (e.g. Atlas).
- The bot needs outbound HTTPS to Telegram's servers and to MongoDB, and an inbound port
  (default 8080, or behind a reverse proxy terminating TLS) for the streaming links in
  `FQDN`/`HAS_SSL`.
- First run creates `*.session` files next to the code; keep them (a volume, or just the
  VPS disk) so the bot doesn't need to log in to Telegram again on every restart.

---

## HTTPS for the streaming links

The player decodes video/audio in the browser via WebCodecs, which browsers only expose on a
secure context -- HTTPS, or the `localhost`/`127.0.0.1` loopback exception. A plain
`http://your-ip/...` link loads the page, the stream downloads fine, but nothing ever decodes:
no error, just a frozen frame with the timer running. So once `FQDN` is anything other than
`localhost`/`127.0.0.1`, it needs to be served over HTTPS.

Two ways to get there, same `HAS_SSL=true`/`FQDN` either way:

- **You have a domain:** put a reverse proxy (nginx, Caddy, etc.) in front terminating TLS with
  a real CA-signed certificate (e.g. via Let's Encrypt/certbot), proxying to `PORT` on
  `127.0.0.1`. Leave `SSL_CERTFILE`/`SSL_KEYFILE` unset -- uvicorn keeps speaking plain HTTP
  behind the proxy, same as today.
- **You only have a bare IP:** there's no CA that will sign a certificate for an IP address
  with no domain behind it, so run `scripts/generate_self_signed_cert.sh <your-ip>` and point
  uvicorn at the result directly:
  ```bash
  scripts/generate_self_signed_cert.sh 203.0.113.10
  ```
  then in `config.env`:
  ```
  HAS_SSL=True
  SSL_CERTFILE=certs/cert.pem
  SSL_KEYFILE=certs/key.pem
  PORT=443
  ```
  `PORT=443` needs root (or `setcap cap_net_bind_service`) -- fine for pm2/systemd running as
  root, as this project's other docs already assume. Visitors get a one-time "not trusted"
  browser warning to click through, since nothing signed the cert; that's the unavoidable
  tradeoff of not owning a domain. If `SSL_CERTFILE`/`SSL_KEYFILE` are set but the files are
  missing, the server logs a warning and falls back to plain HTTP rather than failing to start.
