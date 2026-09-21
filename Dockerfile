FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    JSU_DATA_DIR=/data

WORKDIR /app

# curl is used by the container healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# TrueNAS SCALE runs apps as uid/gid 568 ("apps") by default. Override with
# `user:` in compose if your dataset is owned by a different account.
RUN groupadd -g 568 apps \
    && useradd -u 568 -g 568 -M -s /usr/sbin/nologin apps \
    && mkdir -p /data \
    && chown -R 568:568 /data /app

USER 568:568
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
