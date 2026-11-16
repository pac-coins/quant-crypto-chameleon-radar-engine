FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash quantuser

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY config ./config
COPY src ./src

RUN mkdir -p /app/data /app/data/backups /app/logs && chown -R quantuser:quantuser /app
USER quantuser

CMD ["python", "-m", "src.main"]
