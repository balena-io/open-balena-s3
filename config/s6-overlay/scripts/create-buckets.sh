#!/usr/src/env bash
# shellcheck disable=SC1091

set -e

# Redirect all future stdout/stderr to s6-log
exec > >(exec s6-log -b p"create-buckets[$$]:" 1 || true) 2>&1

# Change to working directory
cd /usr/src/app || exit 1

# Load environment variables for this service
source /etc/s6-overlay/scripts/functions.sh
[[ -f "config/env" ]] && load_env_file "config/env"

if [[ -z "${BUCKETS}" ]]; then
    BUCKETS="${1:-}"
fi

# read the list of new buckets we wish to create...
IFS=';' read -ra NEW_BUCKETS <<< "$BUCKETS"

# pull the list of existing buckets...
# - list the buckets in JSON
# - extract the key value
# - remove the last char, a slash in this case
EXISTING_BUCKETS=($(mc ls --json localhost/ | jq .key -r | rev | cut -c 2- | rev))

for bucket in "${NEW_BUCKETS[@]}"; do
    echo "Create bucket: $bucket..."
    if [[ ! " ${EXISTING_BUCKETS[@]} " =~ " ${bucket} " ]]; then
        /sbin/mc mb "localhost/${bucket}"
    else
        echo "Bucket already exists: $bucket"
    fi
done
