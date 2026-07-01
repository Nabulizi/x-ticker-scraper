FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XTS_CONNECT_HEADLESS=1 \
    XTS_OUTPUT_DIR=/app/data/output \
    XTS_SESSION_FILE=/app/data/session.json

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/data/output

EXPOSE 10000

CMD exec gunicorn --worker-class gthread --workers 1 --threads 16 --timeout 600 --bind 0.0.0.0:${PORT:-10000} wsgi:app
