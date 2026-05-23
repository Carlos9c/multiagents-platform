# .NET 8 SDK (LTS)
FROM mcr.microsoft.com/dotnet/sdk:8.0

LABEL org.opencontainers.image.vendor="agente-desarrollador"
LABEL org.opencontainers.image.title="agente-dotnet:8"
LABEL org.opencontainers.image.description=".NET 8 LTS SDK for C# and F# applications: ASP.NET Core, Blazor, console apps, and class libraries"
LABEL agente.catalog.runtime="dotnet"
LABEL agente.catalog.image="agente-dotnet:8"
LABEL agente.catalog.version="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /project
