from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DOCKERFILES_DIR = Path(__file__).parent / "dockerfiles"


@dataclass(frozen=True)
class CatalogEntry:
    """A curated, project-type-specific Docker environment."""

    runtime_type: str
    image_name: str
    dockerfile: str
    description: str


CATALOG: list[CatalogEntry] = [
    # --- Mobile ---
    CatalogEntry(
        runtime_type="android_gradle",
        image_name="agente-flutter:3",
        dockerfile="flutter.Dockerfile",
        description=(
            "Flutter 3.x + Dart + Android SDK 34. "
            "For Flutter mobile apps targeting Android. "
            "Includes the Flutter SDK, Dart toolchain, and a full Android SDK."
        ),
    ),
    CatalogEntry(
        runtime_type="react_native",
        image_name="agente-react-native:0.74",
        dockerfile="react-native.Dockerfile",
        description=(
            "Node.js 22 LTS + Android SDK 34 + JDK 17. "
            "For React Native and Expo projects that build Android APKs. "
            "Provides the JavaScript bundler (Metro/Node) and the Android Gradle build chain."
        ),
    ),
    CatalogEntry(
        runtime_type="android_gradle",
        image_name="agente-android:sdk34",
        dockerfile="android.Dockerfile",
        description=(
            "Android SDK 34 + Gradle 8.x + JDK 17. "
            "For native Android apps written in Kotlin or Java. "
            "Includes Jetpack libraries, build-tools, NDK, and adb."
        ),
    ),
    # --- JVM ---
    CatalogEntry(
        runtime_type="java_gradle",
        image_name="agente-java:21",
        dockerfile="java.Dockerfile",
        description=(
            "JDK 21 LTS + Maven + Gradle wrapper support. "
            "For Java and Kotlin JVM projects: Spring Boot, Quarkus, Micronaut, "
            "desktop applications, and other non-Android JVM workloads."
        ),
    ),
    # --- Systems ---
    CatalogEntry(
        runtime_type="rust_cargo",
        image_name="agente-rust:stable",
        dockerfile="rust.Dockerfile",
        description=(
            "Rust stable + cargo + common native libraries (OpenSSL, SQLite, libpq). "
            "For Rust applications, CLIs, libraries, and WebAssembly targets."
        ),
    ),
    CatalogEntry(
        runtime_type="go",
        image_name="agente-go:1.22",
        dockerfile="go.Dockerfile",
        description=(
            "Go 1.22 + common build tools. "
            "For Go applications, APIs, CLIs, and microservices. "
            "Modules are resolved by `go build`/`go mod tidy` at build time."
        ),
    ),
    CatalogEntry(
        runtime_type="dotnet",
        image_name="agente-dotnet:8",
        dockerfile="dotnet.Dockerfile",
        description=(
            ".NET 8 SDK (LTS). "
            "For C# and F# applications: ASP.NET Core APIs, Blazor, console apps, "
            "class libraries, and worker services."
        ),
    ),
    # --- Web / scripting ---
    CatalogEntry(
        runtime_type="node_npm",
        image_name="agente-node:22",
        dockerfile="node.Dockerfile",
        description=(
            "Node.js 22 LTS + npm + native build tools. "
            "For JavaScript and TypeScript projects: React, Next.js, Vue, Angular, "
            "Express, NestJS, and other Node-based frontends or APIs."
        ),
    ),
    CatalogEntry(
        runtime_type="python_venv",
        image_name="agente-python:3.12",
        dockerfile="python.Dockerfile",
        description=(
            "Python 3.12 + pip + system libraries for compiled packages "
            "(libpq, libssl, libffi, libjpeg, libxml2, libgdal). "
            "For FastAPI, Django, Flask, data science, ML, scripts, and automation."
        ),
    ),
    # --- Hybrid / full-stack ---
    CatalogEntry(
        runtime_type="python_venv",
        image_name="agente-fullstack:py-node",
        dockerfile="fullstack-py-node.Dockerfile",
        description=(
            "Python 3.12 + Node.js 22 LTS. "
            "For monorepos or projects that combine a Python backend (FastAPI, Django, Flask) "
            "with a JavaScript/TypeScript frontend (React, Vue, Next.js) in the same repository."
        ),
    ),
    CatalogEntry(
        runtime_type="java_gradle",
        image_name="agente-fullstack:java-node",
        dockerfile="fullstack-java-node.Dockerfile",
        description=(
            "JDK 21 + Maven + Node.js 22 LTS. "
            "For monorepos that combine a Java/Spring backend with a "
            "JavaScript/TypeScript frontend in the same repository."
        ),
    ),
]

# Index by image_name for O(1) lookup and validation
_CATALOG_BY_IMAGE: dict[str, CatalogEntry] = {e.image_name: e for e in CATALOG}


def get_all_entries() -> list[CatalogEntry]:
    return list(CATALOG)


def get_entry_by_image(image_name: str) -> CatalogEntry | None:
    return _CATALOG_BY_IMAGE.get(image_name)


def catalog_dockerfile_path(entry: CatalogEntry) -> Path:
    """Absolute path to the Dockerfile for building the catalog image locally."""
    return DOCKERFILES_DIR / entry.dockerfile
