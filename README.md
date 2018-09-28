Resin S3
========

## Overview

This repo contains the source for building an S3 service based on the [Minio]() cloud storage service. It is intended to be used by Resin components that require non-Amazon based S3 storage (eg. the DevEnv).

## Building and Installation

Ensure Docker is installed on your machine:

```
docker build -t resin/open-balena-s3:master .
```

Ensure you login to docker using an account with appropriate rights:

`docker push resin/resin-s3:master`
