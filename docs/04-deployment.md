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
docker build -t file-to-link .

docker run -d \
  --name file-to-link \
  --restart unless-stopped \
  -p 8080:8080 \
  --env-file config.env \
  -v file-to-link-logs:/app/logs \
  -v file-to-link-sessions:/app \
  file-to-link
```

Notes:
- `--env-file config.env` passes every variable from `config.env.example` as real environment
  variables; `python-dotenv` only loads `config.env` as a fallback, so either form works.
- The sessions volume keeps `*.session` files (the bot's Telegram login) across container
  restarts — without it, every restart re-authenticates and may hit login rate limits.
- `docker logs file-to-link` shows the startup banner; `logs/info.log` and `logs/error.log`
  live in the mounted `logs` volume (see `docs/01-architecture-and-flow.md` §15).
- If MongoDB is also containerized, add it as a second service (e.g. `docker-compose`) and
  point `MONGO_HOST` at its service name; this repo doesn't ship a compose file since the
  Mongo instance is usually managed separately (Atlas, a shared VPS instance, etc.).

To update: rebuild the image and `docker stop/rm` + re-`run` (or `docker compose up -d
--build` if you wrap this in a compose file).

---

## Option B: VPS with a process manager

Either systemd (recommended on a VPS you control) or PM2 (the `process.json` already in the
repo) keep the bot running and restart it on crash or reboot.

### B1: systemd

```ini
# /etc/systemd/system/file-to-link.service
[Unit]
Description=File-To-Link Telegram bot
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/file-to-link
EnvironmentFile=/opt/file-to-link/config.env
ExecStart=/opt/file-to-link/venv/bin/python -m streamgram
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Setup:
```bash
cd /opt/file-to-link
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.env.example config.env   # then fill it in
sudo systemctl daemon-reload
sudo systemctl enable --now file-to-link
sudo systemctl status file-to-link
journalctl -u file-to-link -f      # follow logs (the app's own logs/ still apply too)
```

### B2: PM2

`process.json` is already in the repo root and just runs `python3 start_bot.py`:

```bash
cd /opt/file-to-link
python3 -m venv venv  # optional; or install deps globally
venv/bin/pip install -r requirements.txt  # if using a venv, also set "interpreter" in
                                           # process.json to venv/bin/python3
cp config.env.example config.env          # then fill it in
pm2 start process.json
pm2 save                                   # persist across reboots
pm2 startup                                # generates the OS boot hook (follow its printed command)
pm2 logs File-To-Link_Bot
```

To update: `git pull`, `venv/bin/pip install -r requirements.txt` if dependencies changed,
then `pm2 restart File-To-Link_Bot` (or `sudo systemctl restart file-to-link` for systemd).

---

## Either way

- MongoDB must be reachable from wherever the bot runs; `MONGO_SCHEMA=mongodb+srv` works for
  a hosted cluster (e.g. Atlas).
- The bot needs outbound HTTPS to Telegram's servers and to MongoDB, and an inbound port
  (default 8080, or behind a reverse proxy terminating TLS) for the streaming links in
  `FQDN`/`HAS_SSL`.
- First run creates `*.session` files next to the code; keep them (a volume, or just the
  VPS disk) so the bot doesn't need to log in to Telegram again on every restart.
