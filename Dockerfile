FROM balena/open-balena-base:20.0.2-s6-overlay

VOLUME /export

ARG TARGETARCH
ENV GO_SHA256_amd64=464b6b66591f6cf055bc5df90a9750bf5fbc9d038722bb84a9d56a2bea974be6
ENV GO_SHA256_arm64=efa97fac9574fc6ef6c9ff3e3758fb85f1439b046573bf434cccb5e012bd00c8
ENV GO_VERSION=1.19
ENV GOPATH=/go

# https://github.com/minio/minio/tags
# renovate: datasource=github-tags depName=minio/minio versioning=regex:^RELEASE\.(?<major>\d{4})-(?<minor>\d{2})-(?<patch>\d{2})
# https://min.io/docs/minio/linux/operations/install-deploy-manage/migrate-fs-gateway.html
ENV MINIO_VERSION=RELEASE.2022-10-24T18-35-07Z
ENV PATH=${PATH}:/usr/local/go/bin

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Get Go and Minio
RUN curl -SLO https://storage.googleapis.com/golang/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& echo "$(eval echo \$GO_SHA256_${TARGETARCH}) go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" | sha256sum -c \
	&& tar xz -C /usr/local -f go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& rm go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& GO111MODULE=on go install github.com/minio/minio@${MINIO_VERSION} \
	&& rm -rf /go/pkg

RUN curl -fsSL -o /sbin/mc https://dl.min.io/client/mc/release/linux-${TARGETARCH}/mc \
	&& chmod +x /sbin/mc \
	&& mkdir -p /root/.mc

# systemd and minio config
COPY config /usr/src/app/config
COPY config/s6-overlay/ /etc/s6-overlay/
RUN chmod +x /etc/s6-overlay/scripts/*

COPY docker-hc /usr/src/app/
