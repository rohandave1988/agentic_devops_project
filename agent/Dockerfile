## Stage 1 — install dependencies
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

## Stage 2 — minimal runtime image
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

# Non-root user (uid 1000) matches chown in the init container.
# The PVC init container runs: chown 1000:1000 /data/agent
# so this user can write the SQLite DB without ever running as root.
RUN useradd -u 1000 -m agent && mkdir -p /data/agent && chown agent:agent /data/agent

USER agent

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
