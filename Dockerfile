# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /srv/app

# Dependencies first so that the layer is cached.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY .env.example ./
RUN mkdir -p /srv/app/data

# App data (OAuth token / SQLite fallback files) lives here.
VOLUME ["/srv/app/data"]

ENV DB_PATH=/srv/app/data/agent.db \
    GMAIL_TOKEN_PATH=/srv/app/data/token.json

RUN useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /srv/app
USER agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

