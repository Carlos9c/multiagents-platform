# Flutter 3.x + Dart + Android SDK 34
FROM ghcr.io/cirruslabs/flutter:3.22.3

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-flutter:3"
LABEL org.opencontainers.image.description="Flutter 3.22 + Dart + Android SDK 34 for Flutter mobile apps targeting Android"
LABEL agente.catalog.runtime="android_gradle"
LABEL agente.catalog.image="agente-flutter:3"
LABEL agente.catalog.version="1"

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN yes | sdkmanager --licenses > /dev/null 2>&1 || true

WORKDIR /project
