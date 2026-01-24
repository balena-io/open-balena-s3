FROM balena/open-balena-base:20.2.7-s6-overlay

VOLUME /export

ARG TARGETARCH
ENV GO_SHA256_amd64=dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858
ENV GO_SHA256_arm64=c3fa6d16ffa261091a5617145553c71d21435ce547e44cc6dfb7470865527cc7
ENV GO_VERSION=1.24.0
ENV GOPATH=/go

# https://github.com/minio/minio/tags
# renovate: datasource=github-tags depName=minio/minio versioning=regex:^RELEASE\.(?<major>\d{4})-(?<minor>\d{2})-(?<patch>\d{2})
# https://min.io/docs/minio/linux/operations/install-deploy-manage/migrate-fs-gateway.html
ENV MINIO_VERSION=RELEASE.2025-09-07T16-13-09Z
ENV PATH=${PATH}:/usr/local/go/bin

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Get Go and Minio
RUN curl -SLO https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
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
