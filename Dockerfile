FROM python:3.11-slim

WORKDIR /app

# gcc: TgCrypto's build and psutil at runtime. ffmpeg: streamgram/utils/audio_fix.py's AC3/E-AC3/
# DTS/TrueHD -> AAC audio fix (optional at runtime, controlled by ENABLE_AUDIO_FIX).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc ffmpeg \
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
