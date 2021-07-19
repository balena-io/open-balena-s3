#!/bin/bash

# backwards compatible env rename
export MINIO_ROOT_USER="$S3_MINIO_ACCESS_KEY"
export MINIO_ROOT_PASSWORD="$S3_MINIO_SECRET_KEY"

# use semicolon (;) separated env var BUCKETS to create default buckets
IFS=';' read -ra NEW_BUCKETS <<< "$BUCKETS"
for bucket in "${NEW_BUCKETS[@]}"; do
    if [[ ! -d "/export/.minio.sys/$bucket" ]]; then
        mkdir -p "/export/$bucket" "/export/.minio.sys/buckets/$bucket";
    fi
done

exec /usr/bin/docker-entrypoint.sh server --address ":80" --console-address ":9090" /export
