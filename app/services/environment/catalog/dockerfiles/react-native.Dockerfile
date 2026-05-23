# React Native: Node 22 LTS + Android SDK 34 + JDK 17
FROM mingc/android-build-box:1.26.0

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-react-native:0.74"
LABEL org.opencontainers.image.description="Node.js 22 LTS + Android SDK 34 + JDK 17 for React Native and Expo projects building Android APKs"
LABEL agente.catalog.runtime="react_native"
LABEL agente.catalog.image="agente-react-native:0.74"
LABEL agente.catalog.version="1"

RUN curl -fsSL https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-x64.tar.gz \
    | tar -xz -C /usr/local --strip-components=1 \
    && node --version && npm --version

RUN node --version && java -version

WORKDIR /project
