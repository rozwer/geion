FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl wget gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY scraping.py /app/
COPY backend /app/backend

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8080"]
