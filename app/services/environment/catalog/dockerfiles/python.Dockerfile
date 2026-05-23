# Python 3.12 + pip + system libraries needed by common Python packages
FROM python:3.12-slim

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-python:3.12"
LABEL org.opencontainers.image.description="Python 3.12 runtime with system libraries for compiled packages (libpq, libssl, libffi, libjpeg, libxml2, libgdal)"
LABEL agente.catalog.runtime="python_venv"
LABEL agente.catalog.image="agente-python:3.12"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        libpq-dev \
        libxml2-dev \
        libxslt1-dev \
        libjpeg-dev \
        zlib1g-dev \
        libgdal-dev \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /project
