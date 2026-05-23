# Java 21 LTS + Maven + Gradle wrapper support
FROM eclipse-temurin:21-jdk-noble

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-java:21"
LABEL org.opencontainers.image.description="JDK 21 LTS with Maven and Gradle wrapper support for Spring Boot, Quarkus, and other JVM workloads"
LABEL agente.catalog.runtime="java_gradle|java_maven"
LABEL agente.catalog.image="agente-java:21"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        maven \
        git \
        curl \
        unzip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /project
