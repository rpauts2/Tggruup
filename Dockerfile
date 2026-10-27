FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# fonts matter: without a real TTF the cover renderer falls back to a bitmap font
# and Cyrillic covers come out as boxes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        fonts-dejavu-core \
        fonts-liberation \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY tgfarm ./tgfarm
COPY config ./config

RUN pip install --upgrade pip && pip install .

RUN mkdir -p /app/data/media

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["tgfarm", "serve"]