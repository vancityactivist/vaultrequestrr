FROM python:3.12-slim

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
