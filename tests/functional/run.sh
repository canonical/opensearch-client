#!/usr/bin/env bash
#
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Run the opensearch_client functional tests against an ephemeral Docker OpenSearch.
#
# The same script is used locally and in CI so the two behave identically: it
# starts a single-node OpenSearch, waits for it to answer, runs the tests through
# tox, then removes every resource it created. Data lives in the container's
# writable layer and the teardown runs on every exit path, so a run leaves no
# trace on the host.
#
# Environment variables:
#   KEEP_IMAGE=1  Keep the pulled image after the run (default; faster local
#                 iteration). Set to 0 to delete it on teardown.
#
# Requirements: docker (with the compose plugin), curl, openssl, and tox.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
PROJECT="opensearch_client-functional"
URL="http://localhost:9200"
KEEP_IMAGE="${KEEP_IMAGE:-1}"

# Credentials for the security plugin: the demo admin user, whose password is
# generated fresh for each run (unless overridden) and handed to the container
# via OPENSEARCH_INITIAL_ADMIN_PASSWORD. openssl's base64 alphabet contains no
# ':' or '$', so the value is safe for curl's -u and for compose interpolation.
USERNAME="${OPENSEARCH_USER:-admin}"
PASSWORD="${OPENSEARCH_PASSWORD:-$(openssl rand -base64 24)}"
export OPENSEARCH_INITIAL_ADMIN_PASSWORD="${PASSWORD}"

COMPOSE=(docker compose -p "${PROJECT}" -f "${HERE}/docker-compose.yml")

cleanup() {
    echo "Tearing down OpenSearch..."
    if [ "${KEEP_IMAGE}" != "1" ]; then
        "${COMPOSE[@]}" down -v --remove-orphans --rmi all >/dev/null 2>&1 || true
    else
        "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

echo "Starting OpenSearch..."
"${COMPOSE[@]}" up -d

echo "Waiting for OpenSearch at ${URL}..."
for _ in $(seq 1 60); do
    if curl -sf -u "${USERNAME}:${PASSWORD}" "${URL}/_cluster/health" >/dev/null 2>&1; then
        echo "OpenSearch is up"
        break
    fi
    sleep 2
done

echo "Running functional tests..."
cd "${REPO_ROOT}"
OPENSEARCH_URL="${URL}" \
    OPENSEARCH_USER="${USERNAME}" \
    OPENSEARCH_PASSWORD="${PASSWORD}" \
    tox run -e functional
