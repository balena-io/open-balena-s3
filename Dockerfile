FROM balena/open-balena-base:no-systemd-13.0.4

EXPOSE 80

VOLUME /export

ENV GO_SHA256 013a489ebb3e24ef3d915abe5b94c3286c070dfe0818d5bca8108f1d6e8440d2
ENV GO_VERSION 1.16
ENV GOPATH /go
# https://github.com/minio/minio/tree/RELEASE.2022-05-04T07-45-27Z
ENV MINIO_RELEASE=RELEASE.2022-05-04T07-45-27Z
ENV PATH ${PATH}:/usr/local/go/bin

# Get Go and Minio
RUN curl -SLO https://storage.googleapis.com/golang/go${GO_VERSION}.linux-amd64.tar.gz && \
    echo "${GO_SHA256} go${GO_VERSION}.linux-amd64.tar.gz" > go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    sha256sum -c go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    tar xz -C /usr/local -f go${GO_VERSION}.linux-amd64.tar.gz && \
    rm go${GO_VERSION}.linux-amd64.tar.gz go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    GO111MODULE=on go get github.com/minio/minio@${MINIO_RELEASE} && \
    rm -rf /go/pkg

RUN wget -O /sbin/mc https://dl.min.io/client/mc/release/linux-amd64/mc && \
    chmod +x /sbin/mc && \
    mkdir -p /root/.mc

# systemd and minio config
COPY config /usr/src/app/config

# create-buckets
COPY scripts/create-buckets.sh /sbin/create-buckets.sh

COPY docker-hc /usr/src/app/
COPY entry.sh /usr/src/app/
RUN chmod +x /usr/src/app/entry.sh

# Start Minio
CMD [ "/usr/src/app/entry.sh" ]
ENTRYPOINT [ "/usr/src/app/entry.sh" ]