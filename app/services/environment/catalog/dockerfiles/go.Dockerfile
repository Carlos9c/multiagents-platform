# Go 1.22 + common system libraries
FROM golang:1.22-bookworm

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-go:1.22"
LABEL org.opencontainers.image.description="Go 1.22 with build tools for APIs, CLIs, and microservices"
LABEL agente.catalog.runtime="go"
LABEL agente.catalog.image="agente-go:1.22"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libssl-dev \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV GO111MODULE=on
ENV GOPATH=/go
ENV PATH="$GOPATH/bin:$PATH"

WORKDIR /project
