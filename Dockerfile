FROM balena/open-balena-base:22.0.6-s6-overlay@sha256:3986a4e5d437bda24fa1f0014997de7698537989afce9b866fa6ce38828253dd

VOLUME /export

ARG TARGETARCH
ENV GO_SHA256_amd64=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
ENV GO_SHA256_arm64=a06e99309dbcbdca1a8ccd2282c3ee146e55930886e26b4f1193269587165f49
ENV GO_VERSION=1.27.3
ENV GOPATH=/go

# https://github.com/pgsty/silo/tags
# renovate: datasource=github-tags depName=pgsty/silo versioning=regex:^RELEASE\.(?<major>\d{4})-(?<minor>\d{2})-(?<patch>\d{2})
ENV SILO_VERSION=RELEASE.2026-10-26T00-00-00Z

# https://github.com/pgsty/mc/tags
# renovate: datasource=github-tags depName=pgsty/mc versioning=regex:^RELEASE\.(?<major>\d{4})-(?<minor>\d{2})-(?<patch>\d{2})
ENV MC_VERSION=RELEASE.2026-10-26T00-00-00Z

ENV PATH=${PATH}:/usr/local/go/bin

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Get Go
RUN curl -SLO https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& echo "$(eval echo \$GO_SHA256_${TARGETARCH}) go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" | sha256sum -c \
	&& tar xz -C /usr/local -f go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& rm go${GO_VERSION}.linux-${TARGETARCH}.tar.gz

# Build silo from the release source archive
# (the release archive has no .git, so skip the Makefile's git-based ldflags generation)
RUN mkdir -p /go/bin "${GOPATH}/src/silo" \
	&& curl -fsSL https://github.com/pgsty/silo/archive/refs/tags/${SILO_VERSION}.tar.gz \
		| tar xz -C "${GOPATH}/src/silo" --strip-components=1 \
	&& cd "${GOPATH}/src/silo" \
	&& CGO_ENABLED=0 go build -trimpath -tags kqueue -ldflags "-s -w" -o /go/bin/silo . \
	&& cd / \
	&& rm -rf "${GOPATH}/src" /go/pkg /root/.cache/go-build

# Build mc from the release source archive
RUN mkdir -p "${GOPATH}/src/mc" \
	&& curl -fsSL https://github.com/pgsty/mc/archive/refs/tags/${MC_VERSION}.tar.gz \
		| tar xz -C "${GOPATH}/src/mc" --strip-components=1 \
	&& cd "${GOPATH}/src/mc" \
	&& CGO_ENABLED=0 go build -trimpath -tags kqueue -ldflags "-s -w" -o /sbin/mc . \
	&& cd / \
	&& rm -rf "${GOPATH}/src" /go/pkg /root/.cache/go-build \
	&& mkdir -p /root/.mc

# systemd and silo config
COPY config /usr/src/app/config
COPY config/s6-overlay/ /etc/s6-overlay/
RUN chmod +x /etc/s6-overlay/scripts/*

COPY docker-hc /usr/src/app/
