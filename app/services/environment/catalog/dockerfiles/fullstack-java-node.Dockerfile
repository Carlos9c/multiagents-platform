# Full-stack: Java 21 + Node 22 LTS
# For monorepos with a Java/Spring backend and a JavaScript/TypeScript frontend.
FROM eclipse-temurin:21-jdk-noble

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-fullstack:java-node"
LABEL org.opencontainers.image.description="JDK 21 + Maven + Node.js 22 LTS for monorepos combining a Java/Spring backend with a JS/TS frontend"
LABEL agente.catalog.runtime="java_gradle|java_maven"
LABEL agente.catalog.image="agente-fullstack:java-node"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        maven \
        build-essential \
        git \
        curl \
        ca-certificates \
        gnupg \
        unzip \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /project
