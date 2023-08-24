FROM balena/open-balena-base:v15.0.4

EXPOSE 80

VOLUME /export

ARG TARGETARCH

ENV GO_SHA256_amd64 013a489ebb3e24ef3d915abe5b94c3286c070dfe0818d5bca8108f1d6e8440d2
ENV GO_SHA256_arm64 3770f7eb22d05e25fbee8fb53c2a4e897da043eb83c69b9a14f8d98562cd8098
ENV GO_VERSION 1.16
ENV GOPATH /go
# https://github.com/minio/minio/tree/RELEASE.2022-05-04T07-45-27Z
ENV MINIO_RELEASE=RELEASE.2022-05-04T07-45-27Z
ENV PATH ${PATH}:/usr/local/go/bin

# Get Go and Minio
RUN curl -SLO https://storage.googleapis.com/golang/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& echo "$(eval echo \$GO_SHA256_${TARGETARCH}) go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" | sha256sum -c \
	&& tar xz -C /usr/local -f go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& rm go${GO_VERSION}.linux-${TARGETARCH}.tar.gz \
	&& GO111MODULE=on go get github.com/minio/minio@${MINIO_RELEASE} \
	&& rm -rf /go/pkg

RUN wget -O /sbin/mc https://dl.min.io/client/mc/release/linux-${TARGETARCH}/mc \
	&& chmod +x /sbin/mc \
	&& mkdir -p /root/.mc

# systemd and minio config
COPY config /usr/src/app/config
COPY config/services/ /etc/systemd/system/

# create-buckets
COPY scripts/create-buckets.sh /sbin/create-buckets.sh
RUN systemctl enable create-buckets.service

COPY docker-hc /usr/src/app/

# Enable Minio service
RUN systemctl enable open-balena-s3.service
