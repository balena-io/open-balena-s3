#!/bin/bash
set -e

VERSION=$(git rev-parse --short HEAD)
ESCAPED_BRANCH_NAME=$(echo $sourceBranch | sed 's/[^a-z0-9A-Z_.-]/-/g')

# Try pulling the old build first for caching purposes.
docker pull resin/resin-s3-minio:${ESCAPED_BRANCH_NAME} || docker pull resin/resin-s3-minio:master || true

docker build --pull --tag resin/resin-s3-minio:${VERSION} .

docker tag --force resin/resin-s3-minio:${VERSION} resin/resin-s3-minio:${ESCAPED_BRANCH_NAME}
docker tag --force resin/resin-s3-minio:${VERSION} resin/resin-s3-minio:latest

# Push the images
docker push resin/resin-s3-minio:${VERSION}
docker push resin/resin-s3-minio:${ESCAPED_BRANCH_NAME}
docker push resin/resin-s3-minio:latest
