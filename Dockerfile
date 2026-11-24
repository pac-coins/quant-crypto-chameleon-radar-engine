FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN useradd --create-home --shell /bin/bash quantuser

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY config ./config
COPY src ./src

RUN mkdir -p /app/data /app/data/backups /app/logs /app/secrets && chown -R quantuser:quantuser /app
USER quantuser

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-m", "src.core.healthcheck"]

CMD ["python", "-m", "src.main"]
