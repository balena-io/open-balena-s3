#!/bin/bash

set -e

if [ -z "${BUCKETS}" ]; then
    BUCKETS="${1:-}"
fi

# read the list of new buckets and their corresponding base64 encoded config file...
IFS=';' read -ra BUCKET_ENTRIES <<< "$BUCKETS"

# pull the list of existing buckets...
# - list the buckets in JSON
# - extract the key value
# - remove the last char, a slash in this case
EXISTING_BUCKETS=($(mc ls --json localhost/ | jq .key -r | rev | cut -c 2- | rev))

for entry in "${BUCKET_ENTRIES[@]}"; do
    # Split each entry into bucket name and base64 encoded config file (if available)
    IFS=':' read -r bucket encoded_config <<< "$entry"

    echo "Create bucket: $bucket..."
    if [[ ! " ${EXISTING_BUCKETS[@]} " =~ " ${bucket} " ]]; then
        /sbin/mc mb "localhost/${bucket}"
    else
        echo "Bucket already exists: $bucket"
    fi

    if [ -n "$encoded_config" ]; then
        echo "$encoded_config" | base64 -d > $bucket.json
        echo "Applying config:" 
        cat $bucket.json
        echo "to bucket: $bucket..."

        /sbin/mc anonymous set-json $bucket.json "localhost/${bucket}"
    else
        echo "Config not set for bucket: $bucket"
    fi
done
