# Trenchr container image
FROM python:3.11-slim

# small, predictable runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MEMEBOT_HOST=0.0.0.0 \
    MEMEBOT_PORT=8000 \
    MEMEBOT_DB=/data/memeradar.db

WORKDIR /app

# install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# application code
COPY app ./app
COPY scripts ./scripts
COPY config.yaml run.py ./

# persistent data lives on a mounted volume
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# container healthcheck — a 401 (auth on) still means the app is up & serving
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]

CMD ["python", "run.py"]
