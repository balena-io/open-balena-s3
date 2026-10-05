FROM rustfs/rustfs:1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c AS rustfs
FROM rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5 AS rclone
FROM balena/open-balena-base:21.0.35-s6-overlay@sha256:70ff6cf049b10af13fcc31f77e4001e74f916e730f25aa4a1a938d3a5e9defdf

ARG TARGETARCH
ENV CLEANUP_AFTER_MIGRATION=false
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
	&& apt-get install --no-install-recommends -y python3 ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

COPY --from=rustfs /usr/bin/rustfs /usr/local/bin/rustfs
COPY --from=rustfs /lib/ld-musl-*.so.1 /lib/
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

# These readers are used privately during upgrade, never as the public server.
RUN case "${TARGETARCH}" in \
	amd64) FS_SHA=83136a6b903b081a33bdd15d5a82e10d347a5f477f344ee9191a71c06cef4a6f; \
		XL_SHA=7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f ;; \
	arm64) FS_SHA=d9cdceb91c27c86f60b10a284c9602d5e081401bac04e9cad00210e40fad5563; \
		XL_SHA=5c83cd2cf151717ba0243f73e1c7802ff36e272b67144bdd7f1f7d684fd6f03d ;; \
	*) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
	esac \
	&& curl -fSL --retry 3 -o /usr/local/bin/minio-fs \
		"https://github.com/minio/minio/releases/download/RELEASE.2022-10-24T18-35-07Z/minio.linux-${TARGETARCH}.RELEASE.2022-10-24T18-35-07Z" \
	&& echo "${FS_SHA} /usr/local/bin/minio-fs" | sha256sum -c \
	&& curl -fSL --retry 3 -o /usr/local/bin/minio-xl \
		"https://github.com/minio/minio/releases/download/RELEASE.2025-09-07T16-13-09Z/minio.linux-${TARGETARCH}.RELEASE.2025-09-07T16-13-09Z" \
	&& echo "${XL_SHA} /usr/local/bin/minio-xl" | sha256sum -c \
	&& chmod 755 /usr/local/bin/minio-fs /usr/local/bin/minio-xl

# Retain upstream licenses and matching source for the unmodified readers.
RUN mkdir -p /usr/share/open-balena-s3/source \
	&& curl -fSL --retry 3 -o /usr/share/open-balena-s3/source/minio-fs.tar.gz \
		https://codeload.github.com/minio/minio/tar.gz/fc6c7949727ec261cd57fbdb02fa7575d0fd8e61 \
	&& curl -fSL --retry 3 -o /usr/share/open-balena-s3/source/minio-xl.tar.gz \
		https://codeload.github.com/minio/minio/tar.gz/07c3a429bfed433e49018cb0f78a52145d4bedeb \
	&& curl -fSL --retry 3 -o /usr/share/open-balena-s3/source/RustFS-LICENSE \
		https://raw.githubusercontent.com/rustfs/rustfs/6de965ae3c965a78ff819fbcd7acd4aa44177d92/LICENSE \
	&& tar -xOf /usr/share/open-balena-s3/source/minio-fs.tar.gz \
		minio-fc6c7949727ec261cd57fbdb02fa7575d0fd8e61/LICENSE \
		> /usr/share/open-balena-s3/source/MinIO-LICENSE

VOLUME /export
COPY config /usr/src/app/config
COPY config/s6-overlay/ /etc/s6-overlay/
COPY migration /usr/src/app/migration
RUN chmod +x /etc/s6-overlay/scripts/*
COPY docker-hc /usr/src/app/

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=15s --start-period=24h --retries=3 \
	CMD /usr/src/app/docker-hc
