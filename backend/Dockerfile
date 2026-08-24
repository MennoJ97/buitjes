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
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3000/healthz || exit 1
CMD ["/app/weather-backend"]
