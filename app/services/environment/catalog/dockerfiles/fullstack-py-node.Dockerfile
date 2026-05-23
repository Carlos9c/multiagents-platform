# Full-stack: Python 3.12 + Node 22 LTS
# For monorepos with a Python backend and a JavaScript/TypeScript frontend.
FROM python:3.12-slim

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-fullstack:py-node"
LABEL org.opencontainers.image.description="Python 3.12 + Node.js 22 LTS for monorepos combining a Python backend with a JS/TS frontend"
LABEL agente.catalog.runtime="python_venv"
LABEL agente.catalog.image="agente-fullstack:py-node"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        libpq-dev \
        libxml2-dev \
        libjpeg-dev \
        git \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /project
