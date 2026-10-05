#!/usr/bin/env bash
# shellcheck disable=SC1091

set -euo pipefail

# Redirect all future stdout/stderr to s6-log
exec > >(exec s6-log p"create-buckets[$$]:" 1 || true) 2>&1

# Change to working directory
cd /usr/src/app || exit 1

# Load environment variables for this service
set -a
source /etc/docker.env
set +a

export BUCKETS="${BUCKETS:-${1:-}}"
exec python3 /usr/src/app/migration/bootstrap.py buckets
