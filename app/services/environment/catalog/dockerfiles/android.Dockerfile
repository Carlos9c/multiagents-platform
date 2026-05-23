# Android SDK 34 + Gradle 8.x + OpenJDK 17
# Built on top of mingc/android-build-box which ships:
#   JDK 17, Android SDK API 21-34, build-tools, NDK, Gradle, and adb.
FROM mingc/android-build-box:1.26.0

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-android:sdk34"
LABEL org.opencontainers.image.description="Android SDK 34 + Gradle 8.x + JDK 17 for native Android apps in Kotlin or Java"
LABEL agente.catalog.runtime="android_gradle"
LABEL agente.catalog.image="agente-android:sdk34"
LABEL agente.catalog.version="1"

WORKDIR /project
