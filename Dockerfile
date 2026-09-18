# Lightweight image: the browser runs in Browser-Use Cloud, so we only need Python + pure-Python
# SDKs (playwright here is a CLIENT that connects to the remote browser over CDP — no local
# Chromium, no `playwright install`). Scheduling is handled inside the container by supercronic.
#
# Builds on both amd64 and arm64 (Raspberry Pi 4/5 on a 64-bit OS). See "Raspberry Pi" in README.md.
FROM python:3.12-slim

# --- supercronic (cron designed for containers) ---
# The binary is per-architecture. TARGETARCH is set automatically by BuildKit (amd64 / arm64 / arm);
# when it isn't (a very old builder), fall back to the base image's own Debian architecture. Getting
# this wrong used to produce an image that BUILT fine and then died on `exec format error` at
# runtime, restart-looping forever with nothing to alert on — so the download is smoke-tested below.
ARG SUPERCRONIC_VERSION=v0.2.33
ARG TARGETARCH
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64|arm64|arm|386) ;; \
        armhf) arch=arm ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}" \
        -o /usr/local/bin/supercronic; \
    chmod +x /usr/local/bin/supercronic; \
    # Smoke-test: proves we fetched a runnable binary for THIS architecture. A wrong-arch or
    # truncated download fails the build here, loudly, instead of at 03:00 on the Pi.
    /usr/local/bin/supercronic -version; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements-web.txt is the read-only dashboard's stack (FastAPI, Jinja2, uvicorn). It rides in
# this one image on purpose -- the entrypoint starts it
# beside the scheduler when WEB_ENABLED is true. It is still a separate file so `python -m web` on a
# desktop stays an opt-in install.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# App code only — secrets/config are excluded via .dockerignore and provided at runtime.
COPY . .

COPY docker/entrypoint.sh docker/run_once.sh docker/backup_once.sh docker/healthcheck.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/run_once.sh /usr/local/bin/backup_once.sh /usr/local/bin/healthcheck.sh

# Unbuffered so `docker compose logs -f` shows a run as it happens rather than in one burst at the end.
ENV PYTHONUNBUFFERED=1

# The dashboard. Bound to 0.0.0.0 INSIDE the container; docker-compose.yml decides which host
# interface publishes it (127.0.0.1 unless WEB_PUBLISH_HOST says otherwise).
EXPOSE 8765

HEALTHCHECK --interval=15m --timeout=30s --start-period=2m \
    CMD /usr/local/bin/healthcheck.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
