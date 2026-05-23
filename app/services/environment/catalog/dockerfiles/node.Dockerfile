# Node.js 22 LTS + Python3 + native build tools
# Python3 and build-essential are required for node-gyp (native add-ons).
FROM node:22-slim

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-node:22"
LABEL org.opencontainers.image.description="Node.js 22 LTS with Python3 and native build tools for node-gyp"
LABEL agente.catalog.runtime="node_npm"
LABEL agente.catalog.image="agente-node:22"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        build-essential \
        make \
        libssl-dev \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /project
