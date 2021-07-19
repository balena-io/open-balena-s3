FROM minio/minio:RELEASE.2021-07-08T01-15-01Z

COPY start.sh .

VOLUME ["/export"]

ENTRYPOINT ["sh", "start.sh"]
