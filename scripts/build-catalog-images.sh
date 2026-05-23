#!/usr/bin/env bash
# Build and smoke-test all agente-desarrollador catalog Docker images.
#
# Usage (from project root):
#   bash scripts/build-catalog-images.sh            # build all
#   bash scripts/build-catalog-images.sh python node # build only those two
#
# Requirements: Docker daemon running, internet access for base image pulls.
#
# Image sizes (approximate download for base layers):
#   Light  (<200 MB): python, node, dotnet
#   Medium (200-800 MB): java, rust, go, fullstack-py-node, fullstack-java-node
#   Heavy  (3-5 GB):  android, flutter, react-native  ← budget time accordingly

set -euo pipefail

CATALOG_DIR="app/services/environment/catalog/dockerfiles"
VENDOR="agente-desarrollador"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

pass() { echo -e "${GREEN}✓${RESET} $*"; }
fail() { echo -e "${RED}✗${RESET} $*"; }
info() { echo -e "${YELLOW}→${RESET} $*"; }
header() { echo -e "\n${BOLD}$*${RESET}"; }

build_and_verify() {
    local key="$1"
    local tag="$2"
    local dockerfile="$3"
    local smoke_cmd="$4"
    local shell="${5:-sh}"   # sh or bash

    header "[$key] $tag"
    info "Building from $dockerfile ..."

    if docker build \
        --label "agente.catalog.built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        -t "$tag" \
        -f "$CATALOG_DIR/$dockerfile" \
        "$CATALOG_DIR" \
        2>&1 | tail -5; then
        pass "Build OK"
    else
        fail "Build FAILED"
        FAILURES+=("$tag (build)")
        return
    fi

    info "Smoke test: $smoke_cmd"
    if docker run --rm "$tag" "$shell" -c "$smoke_cmd" > /dev/null 2>&1; then
        pass "Smoke test OK"
    else
        fail "Smoke test FAILED — run manually:"
        echo "    docker run --rm $tag $shell -c \"$smoke_cmd\""
        FAILURES+=("$tag (smoke)")
    fi
}

# -------------------------------------------------------------------
# Catalog entries: key | image_tag | dockerfile | smoke_command | shell
# -------------------------------------------------------------------
declare -A ENTRIES_TAG
declare -A ENTRIES_DF
declare -A ENTRIES_SMOKE
declare -A ENTRIES_SHELL

register() {
    ENTRIES_TAG["$1"]="$2"
    ENTRIES_DF["$1"]="$3"
    ENTRIES_SMOKE["$1"]="$4"
    ENTRIES_SHELL["$1"]="${5:-sh}"
    ORDER+=("$1")
}

ORDER=()

register python       "agente-python:3.12"           "python.Dockerfile"              "python --version && pip --version"
register node         "agente-node:22"                "node.Dockerfile"                "node --version && npm --version"
register java         "agente-java:21"                "java.Dockerfile"                "java --version && mvn --version"
register rust         "agente-rust:stable"            "rust.Dockerfile"                "rustc --version && cargo --version"
register go           "agente-go:1.22"                "go.Dockerfile"                  "go version"
register dotnet       "agente-dotnet:8"               "dotnet.Dockerfile"              "dotnet --version"
register android      "agente-android:sdk34"          "android.Dockerfile"             "java -version && echo \$ANDROID_HOME" "bash"
register flutter      "agente-flutter:3"              "flutter.Dockerfile"             "flutter --version"                    "bash"
register reactnative  "agente-react-native:0.74"      "react-native.Dockerfile"        "node --version && java -version"      "bash"
register pynodestack  "agente-fullstack:py-node"      "fullstack-py-node.Dockerfile"   "python --version && node --version"
register javanodestack "agente-fullstack:java-node"   "fullstack-java-node.Dockerfile" "java --version && node --version"

# -------------------------------------------------------------------
# Decide which images to build
# -------------------------------------------------------------------
if [ ${#} -eq 0 ]; then
    TARGETS=("${ORDER[@]}")
else
    TARGETS=("$@")
fi

FAILURES=()
BUILT=0

echo -e "\n${BOLD}agente-desarrollador catalog image builder${RESET}"
echo "Vendor label : $VENDOR"
echo "Catalog dir  : $CATALOG_DIR"
echo "Targets      : ${TARGETS[*]}"

for key in "${TARGETS[@]}"; do
    if [ -z "${ENTRIES_TAG[$key]+_}" ]; then
        fail "Unknown image key '$key'. Valid keys: ${ORDER[*]}"
        exit 1
    fi
    build_and_verify \
        "$key" \
        "${ENTRIES_TAG[$key]}" \
        "${ENTRIES_DF[$key]}" \
        "${ENTRIES_SMOKE[$key]}" \
        "${ENTRIES_SHELL[$key]}"
    BUILT=$((BUILT + 1))
done

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
header "Summary"
echo "Built : $BUILT image(s)"

if [ ${#FAILURES[@]} -eq 0 ]; then
    pass "All images OK"
else
    fail "${#FAILURES[@]} failure(s):"
    for f in "${FAILURES[@]}"; do echo "  - $f"; done
    exit 1
fi

echo ""
echo "To list all agente-desarrollador images:"
echo "  docker images --filter 'label=org.opencontainers.image.vendor=agente-desarrollador'"
