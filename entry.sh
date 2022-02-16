#!/bin/bash

# Run confd --onetime
confd -onetime -confdir /usr/src/app/config/confd -backend env

set -m

/bin/mkdir -p /export/.minio.sys/config && \ 
/bin/cp /usr/src/app/minio-config.json /export/.minio.sys/config/config.json && \
/go/bin/minio server --address ":80" --console-address ":43697" /export & \
/sbin/create-buckets.sh

fg %1
