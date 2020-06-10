FROM balena/open-balena-base:v9.4.0

EXPOSE 80

VOLUME /export

ENV GO_VERSION 1.13.3
ENV GO_SHA256 0804bf02020dceaa8a7d7275ee79f7a142f1996bfd0c39216ccb405f93f994c0
ENV PATH ${PATH}:/usr/local/go/bin
ENV GOPATH /go

# Get Go and Minio
RUN curl -SLO https://storage.googleapis.com/golang/go${GO_VERSION}.linux-amd64.tar.gz && \
    echo "${GO_SHA256} go${GO_VERSION}.linux-amd64.tar.gz" > go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    sha256sum -c go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    tar xz -C /usr/local -f go${GO_VERSION}.linux-amd64.tar.gz && \
    rm go${GO_VERSION}.linux-amd64.tar.gz go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    GO111MODULE=on go get github.com/minio/minio && \
    rm -rf /go/pkg

RUN wget -O /sbin/mc https://dl.min.io/client/mc/release/linux-amd64/mc && \
    chmod +x /sbin/mc && \
    mkdir -p /root/.mc

# systemd and minio config
COPY config /usr/src/app/config
COPY config/services/ /etc/systemd/system/

# create-buckets
COPY scripts/create-buckets.sh /sbin/create-buckets.sh
RUN systemctl enable create-buckets.service

# Enable Minio service
RUN systemctl enable open-balena-s3.service
