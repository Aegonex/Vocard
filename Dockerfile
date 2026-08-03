FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 vocard \
    && useradd --uid 10001 --gid vocard --create-home --shell /usr/sbin/nologin vocard \
    && mkdir -p /app \
    && chown vocard:vocard /app

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

COPY --chown=vocard:vocard . .

USER vocard

STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
