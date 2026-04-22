# Clash Dashboard - Bundled Mihomo Edition
# Single container: mihomo (Clash Meta) + FastAPI dashboard
#
# Build:
#   docker build -t clash-dashboard .
#
# Run:
#   docker run --privileged -p 8080:8080 -p 7890:7890 -p 7891:7891 \
#              -v ./clash-config:/app/clash-config \
#              clash-dashboard
#
# Compose:
#   docker compose up

FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Download mihomo binary (linux-amd64)
ARG MIHOMO_VERSION=v1.19.24
RUN curl -fSL \
    "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-amd64-compatible-${MIHOMO_VERSION}.gz" \
    -o mihomo-linux-amd64.gz \
    && gunzip mihomo-linux-amd64.gz \
    && chmod +x mihomo-linux-amd64

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

# Runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built mihomo from builder
COPY --from=builder /app/mihomo-linux-amd64 /usr/local/bin/mihomo
RUN chmod +x /usr/local/bin/mihomo

# Copy application
COPY backend/           /app/backend/
COPY frontend/          /app/frontend/
COPY default-config.yaml /app/clash-config/config.yaml

# Install Python deps
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] \
    httpx aiofiles pyyaml python-multipart

# Environment defaults
ENV MIHOMO_API_PORT=9090
ENV MIHOMO_SOCKS_PORT=7890
ENV CLASH_API_URL=http://127.0.0.1:9090
ENV STATIC_DIR=/app/frontend
ENV PYTHONUNBUFFERED=1

# Expose ports
#   8080  - Dashboard WebUI
#   9090  - Mihomo RESTful API
#   7890  - HTTP proxy
#   7891  - SOCKS5 proxy
EXPOSE 8080 9090 7890 7891

WORKDIR /app/backend

# Healthcheck
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -sf http://127.0.0.1:8080/api/health || exit 1

# Entrypoint: launcher manages mihomo + backend lifecycle
ENTRYPOINT ["python", "launcher.py"]
