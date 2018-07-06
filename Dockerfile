FROM resin/resin-base:v4.2.1
EXPOSE 80

VOLUME /export

ENV GO_VERSION 1.10.3
ENV GO_SHA256 fa1b0e45d3b647c252f51f5e1204aba049cde4af177ef9f2181f43004f901035
ENV PATH ${PATH}:/usr/local/go/bin
ENV GOPATH /go

# Get Go and Minio
RUN curl -SLO https://storage.googleapis.com/golang/go${GO_VERSION}.linux-amd64.tar.gz && \
    echo "${GO_SHA256} go${GO_VERSION}.linux-amd64.tar.gz" > go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    sha256sum -c go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    tar xz -C /usr/local -f go${GO_VERSION}.linux-amd64.tar.gz && \
    rm go${GO_VERSION}.linux-amd64.tar.gz go${GO_VERSION}.linux-amd64.tar.gz.sha256sum && \
    go get -u github.com/minio/minio

# systemd and minio config
COPY config/services/ /etc/systemd/system/
COPY config/config.json /root/.minio/config.json

# Enable Minio service
RUN systemctl enable resin-s3.service
