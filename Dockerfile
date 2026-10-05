FROM python:3.11-slim

WORKDIR /app

# System packages used by TgCrypto's build and by psutil at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
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
