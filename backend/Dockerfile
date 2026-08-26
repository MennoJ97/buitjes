FROM rust:slim AS builder
WORKDIR /app

# Cache the dependency build: compile a stub against Cargo.toml first, so that
# editing src/ does not invalidate the (slow) dependency layer.
COPY backend/Cargo.toml backend/Cargo.lock* ./
RUN mkdir src && echo "fn main() {}" > src/main.rs && cargo build --release; rm -rf src

COPY backend/src ./src
RUN touch src/main.rs && cargo build --release

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/target/release/weather-backend /app/weather-backend
# Bake the frontend into the image so the container runs standalone.
# docker-compose.override.yml bind-mounts it over this copy for local development.
COPY frontend /app/frontend

# Same UID as the ingestor, and for a specific reason: the frames it publishes
# are 0600, so this process reads them by being that user rather than by any
# group membership. See the comment in ingestor/Dockerfile.
#
# Nothing here needs root - /data is mounted read-only, /app is only read, and
# 3000 is above the privileged range so the bind still works.
RUN groupadd --system --gid 10001 buitjes \
    && useradd --system --uid 10001 --gid 10001 --no-create-home buitjes
USER buitjes

EXPOSE 3000
# /livez, never /healthz: an unhealthy container is dropped from Traefik's
# routing table, and /healthz reports on the freshness of KNMI's data. Pointing
# the probe at it means an upstream publishing gap takes the site off the
# internet rather than showing the staleness banner built for it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3000/livez || exit 1
CMD ["/app/weather-backend"]
