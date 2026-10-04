# syntax=docker/dockerfile:1
# ------------------------------------------------------------------------------
#  Telegram-Bot-for-X  -  lightweight production image
# ------------------------------------------------------------------------------
FROM python:3.11-slim

# Fail fast on errors, keep logs unbuffered for platform log collectors.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer between builds.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the application source.
COPY . .

# The SQLite database lives here; docker-compose mounts a volume on top.
RUN mkdir -p /app/data

# Run as an unprivileged user (never run a bot as root).
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Health-check web server port (Render injects PORT=10000 at runtime).
EXPOSE 8000

# Simple container-level health probe against the aiohttp endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
port=os.environ.get('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status==200 else 1)"

CMD ["python", "main.py"]
