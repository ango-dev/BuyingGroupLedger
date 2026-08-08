# Lightweight image: the browser runs in Browser-Use Cloud, so we only need Python + pure-Python
# SDKs (playwright here is a CLIENT that connects to the remote browser over CDP — no local
# Chromium, no `playwright install`). Scheduling is handled inside the container by supercronic.
FROM python:3.12-slim

# --- supercronic (cron designed for containers) ---
# amd64 by default; for ARM hosts change to supercronic-linux-arm64 and its sha.
ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC=supercronic-linux-amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/${SUPERCRONIC}" \
        -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code only — secrets/config are excluded via .dockerignore and provided at runtime.
COPY . .

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
