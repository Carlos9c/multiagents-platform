# Rust stable + common system libraries for native crates
FROM rust:1-slim-bookworm

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-rust:stable"
LABEL org.opencontainers.image.description="Rust stable toolchain with native libraries for OpenSSL, SQLite, libpq and musl cross-compilation"
LABEL agente.catalog.runtime="rust_cargo"
LABEL agente.catalog.image="agente-rust:stable"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libssl-dev \
        libsqlite3-dev \
        libpq-dev \
        git \
        curl \
        ca-certificates \
        musl-tools \
    && rm -rf /var/lib/apt/lists/*

RUN cargo search --limit 0 2>/dev/null || true

WORKDIR /project
