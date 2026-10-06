FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install chromium

COPY app.py .

ENV PROFILE_DIR=/data/chrome_profile \
    PYTHONUNBUFFERED=1

EXPOSE 10000
# Single worker: one shared browser instance
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
