FROM python:3.11-slim

WORKDIR /app

# build-essential + python3-dev: TgCrypto compiles a C extension on install, and needs a
# full gcc + libc + Python.h toolchain, not just gcc alone (confirmed by actually building
# this image -- gcc on its own fails with "stdint.h: No such file or directory"). psutil also
# builds a native extension. ffmpeg: streamgram/utils/audio_fix.py's AC3/E-AC3/DTS/TrueHD ->
# AAC audio fix (optional at runtime, controlled by ENABLE_AUDIO_FIX).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p logs

# config.env, logs/ and the Pyrogram *.session files are runtime state: mount them as a
# volume or override them with --env-file / real environment variables. Never bake real
# credentials into the image.
EXPOSE 8080

CMD ["python", "-m", "streamgram"]
