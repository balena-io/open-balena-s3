FROM resin/resin-base:2

EXPOSE 80

VOLUME /export

ENV GO_VERSION 1.8.3
ENV GO_SHA1 838c415896ef5ecd395dfabde5e7e6f8ac943c8e
ENV PATH ${PATH}:/usr/local/go/bin
ENV GOPATH /go

# Get Go and Minio
RUN curl -SLO https://storage.googleapis.com/golang/go${GO_VERSION}.linux-amd64.tar.gz && \
    echo "${GO_SHA1} go${GO_VERSION}.linux-amd64.tar.gz" > go${GO_VERSION}.linux-amd64.tar.gz.sha1sum && \
    sha1sum -c go${GO_VERSION}.linux-amd64.tar.gz.sha1sum && \
    tar xz -C /usr/local -f go${GO_VERSION}.linux-amd64.tar.gz && \
    rm go${GO_VERSION}.linux-amd64.tar.gz go${GO_VERSION}.linux-amd64.tar.gz.sha1sum && \
    go get -u github.com/minio/minio

# systemd and minio config
COPY config/services/ /etc/systemd/system/
COPY config/config.json /root/.minio/config.json

# Enable Minio service
RUN systemctl enable resin-s3.service
