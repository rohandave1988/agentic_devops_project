FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PYTHONUNBUFFERED=1
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/healthz')"

# gunicorn: 4 sync workers, one thread each — avoids GIL contention from Flask dev server
# --preload ensures background threads (resource metrics, memory leak) start with the app
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "4", "--preload", "--timeout", "120"]
