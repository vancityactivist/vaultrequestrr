FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/vancityactivist/vaultrequestrr" \
      org.opencontainers.image.description="Discord bot that requests media via Seerr with self-service Plex account linking" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vaultrequestrr ./vaultrequestrr

# Persisted SQLite link store lives here; mount a volume to keep it.
ENV DATABASE_PATH=/data/vaultrequestrr.sqlite3
VOLUME ["/data"]

CMD ["python", "-m", "vaultrequestrr"]
