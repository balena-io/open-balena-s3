open-balena-s3
==============

An S3 service based on [RustFS]. It is used by [openBalena] and
[balenaMachine] to provide Amazon S3-compatible storage.

[RustFS]: https://rustfs.com
[balenaMachine]: https://www.balena.io/machine
[openBalena]: https://balena.io/open

## Migration to RustFS

MinIO's community server is no longer maintained. This image switches the public S3 server to
RustFS, an actively developed, Apache-2.0-licensed object store. The goal is to keep the existing
openBalena deployment interface while providing a tested upgrade path for both modern MinIO
single-drive storage and older legacy filesystem installations.

RustFS can reuse the supported MinIO XL on-disk layout, but **it cannot directly open MinIO's
legacy FS layout**. The container inspects the mounted `/export` volume and selects the appropriate
path automatically. It never rewrites `format.json` to pretend the volume uses another format.

### Supported upgrade paths and disk space

| Existing volume | Upgrade behavior | Temporary space requirement |
| --- | --- | --- |
| Empty volume | Initialize RustFS directly in `/export`. | Normal RustFS storage requirements; no duplicate data. |
| MinIO `xl-single`, format version 1, XL version 3, `SIPMOD+PARITY`, exactly one drive | Capture the source inventory through a bundled modern MinIO reader, stop it, start RustFS on the same volume, and verify every imported object. | No bulk object copy and no migration-specific 2x requirement. Allow space for RustFS metadata and the verification manifest. |
| MinIO `fs`, format version 1, FS version 2 | Stage the original store in a separate directory on the same volume, copy through private S3 endpoints, and verify before enabling public access. | **Approximately 2x the stored live data, plus metadata/index and temporary-transfer overhead.** |
| Unknown, malformed, untracked RustFS, multi-drive/distributed, or other MinIO format | Fail explicitly without initializing a replacement store or exposing an incomplete S3 endpoint. | A separately evaluated migration is required. |

**For a legacy-FS volume containing 100 GiB of live objects, plan for at least another 100 GiB
of free space, plus overhead: approximately 200 GiB total or more.** The container checks free
space before copying; insufficient space is an error, not permission to delete source objects
progressively. Resuming a partial copy credits existing size-matching destination objects.

An interrupted copy can require more headroom than the initial copy: replacing an existing target
object may temporarily retain both old and new payloads. Transfers are limited to one object at a
time, and the resumed-copy check reserves space for the largest source object as well as the
remaining copy. In the worst case of one large object already present at the destination, plan
for approximately **3x that object's size**, plus overhead, during its replacement. Existing
unfinished uploads also consume real disk space. The space check is a conservative startup gate,
not a guarantee against filesystem-specific overhead or other processes consuming space.

The native XL path avoids copying object payloads, but still reads the entire source and destination
to verify content. Both upgrade paths therefore require downtime, potentially substantial for
large inventories. Backup or clone the volume before upgrading. A full external backup may itself
require additional capacity; that is separate from the migration's temporary space requirement.

### Deployment compatibility

The image keeps the following interface:

- The same `/export` volume mount and public S3 port **80**.
- `S3_MINIO_ACCESS_KEY` and `S3_MINIO_SECRET_KEY` remain the credential environment variables.
  Their names are retained for existing deployments even though RustFS is now the server.
- `BUCKETS` remains a semicolon-separated list of buckets to create idempotently.
- `S3_REGION` defaults to `us-east-1`; existing `MINIO_SITE_REGION` /
  `MINIO_REGION_NAME` settings are retained when no explicit `S3_REGION` is supplied.
- Existing bucket names and object keys remain unchanged.
- The `docker-hc` command remains the container health check.

Stop the old container before starting the replacement. Do not run an independently managed
MinIO or another storage server against the same volume during upgrade. The new container holds
a volume lock to prevent overlapping instances of its own upgrade coordinator.

No second container, second volume, Kubernetes job, or external migration service is needed.
All migration tools are bundled in the image; first boot does not download a historical server.

### What happens on first boot

The existing s6 service runs the upgrade coordinator instead of exposing MinIO directly:

1. Validate configuration and identify the volume's format. Reject unsupported layouts and
   configurations before cutover.
2. For legacy FS, durably record the source entries and move them using same-filesystem renames
   into `/export/.open-balena-storage/minio-source`. This staging does not copy object payloads.
   Interrupted staging resumes from the recorded entries rather than guessing or reformatting.
3. Start the appropriate checksum-pinned MinIO reader on loopback only. Capture an independent
   inventory, object metadata, tags, and streamed SHA-256 hashes.
4. For native XL reuse, stop the MinIO reader before starting RustFS against the original
   `/export` data. Verify the captured inventory against RustFS without copying the objects.
5. For legacy FS, start RustFS in `/export/.open-balena-storage/rustfs`. Use rclone for resumable,
   non-destructive copying through loopback S3 endpoints. Preserve empty buckets, relevant
   headers/user metadata and tags. Independently verify inventories, sizes and streamed SHA-256
   hashes against the original saved manifest. Multipart ETags alone are not treated as checksums.
6. Write an authenticated storage-identity object, flush the volume, and atomically persist the
   verified completion record.
7. Provision the configured `BUCKETS`, optionally clean the retained FS source, and only then
   open the public S3 endpoint on port 80.

Private MinIO and RustFS listeners are not exposed on the container network. Port 80 is a
byte-preserving relay to RustFS, retaining the original S3 request headers, paths, signatures and
streaming bodies. MinIO stops after migration; it is not the steady-state public server.

The image includes a private control bucket named `open-balena-storage-<store-id>` containing
only `identity.json`. It verifies that a durable completion record still belongs to the actual
RustFS backend. Do not delete or change this bucket, the migration records, or RustFS's internal
directories. The control bucket appears in authenticated bucket listings.

### Restart and recovery behavior

Migration records and the source manifest live under `/export/.open-balena-storage`, not in the
container's writable layer. They contain no storage credentials and are created with restricted
permissions.

If a container stops during staging or copying, the next boot resumes the unfinished operation.
The original FS store is retained until the complete target has been verified. Unexpected extra
objects, malformed state, metadata/hash mismatches, or backend identity loss cause an explicit
failure; the coordinator does not silently report an empty or partial migration as successful.

After completion, restarts start RustFS without recopying the old snapshot. Legitimate subsequent
writes, overwrites, registry garbage collection and deletions are not undone by restoring old
source objects.

An existing completion record is not sufficient by itself: the backend identity must also be
reachable and match before readiness or cleanup. Startup failures are reported in container logs;
private server logs are retained with restricted permissions under `/run/open-balena-storage`
for diagnosis. Preserve the volume and fix the reported cause before retrying.

The built-in health check permits a long initial migration and becomes healthy only after the
verified public endpoint is available. If your orchestrator supplies its own startup/liveness
probes or restart policy, allow enough startup time for the full inventory/copy/verification.
Do not repeatedly kill a healthy, progressing migration because port 80 is still gated.

### Optional cleanup of the retained legacy-FS source

`CLEANUP_AFTER_MIGRATION` defaults to `false`. Accepted values are `0`, `false`, `1`, and `true`
(case-insensitive).

With the default, `/export/.open-balena-storage/minio-source` remains as the original recovery
copy after cutover. **The volume consequently continues to hold both datasets until cleanup
is requested.**

Set the variable to `1` or `true` to delete that retained source only after verified completion
and backend identity validation:

```yaml
environment:
  CLEANUP_AFTER_MIGRATION: "true"
```

You may enable it on the initial upgrade, or enable it on a later restart. Cleanup is evaluated
even when the durable migration-complete record already exists. Its own durable `cleaned` record
is separate from completion; an interrupted cleanup resumes, and completed cleanup is idempotent.
No new migration is run merely because cleanup was enabled later.

This option applies **only to the separately copied legacy-FS source**. It never deletes shared
native-XL payloads or their MinIO metadata, and is a no-op for native reuse and fresh installations.
Cleanup removes the recovery copy and is not reversible.

### Limitations and rollback

This is not a universal MinIO format converter. The initial implementation supports the exact
single-drive layouts listed above and the root-credential deployment interface used by this
repository. Custom MinIO IAM users/service accounts/mappings and unsupported bucket features,
including version history, encryption, retention/object lock and nonempty policies, require a
separately planned migration rather than being silently discarded.

The MinIO Console and MinIO-specific administrative APIs are not retained. RustFS's console is
disabled in this S3-service image; applications should use the existing S3 endpoint, not depend
on MinIO management endpoints.

Some historical MinIO inspection APIs are unavailable even for a valid FS store. Compatibility
exceptions are individually audited and restricted to the exact bundled reader release and
offline-validated source format; an arbitrary `501`, access denial or malformed response is not
treated as evidence that a feature is absent.

Destination inspection also accounts for the pinned RustFS 1.0.1 server's exact default
private-ACL representation, encryption-not-configured response and unimplemented ownership-controls
API. These exceptions are release-scoped; they do not bypass source inspection or permit
custom/public grants or encryption.

Treat native handover as **one-way**. RustFS imports MinIO data but writes its own system metadata;
MinIO is not guaranteed to read RustFS writes. Keeping original files, or keeping an FS source
after copy migration, does not provide transparent rollback once clients have changed the target.
Restore a backup/clone using a separately planned recovery procedure rather than simply downgrading
the image and assuming the layouts are interchangeable.

Both architectures remain supported (`linux/amd64`, `linux/arm64`). RustFS and rclone image stages
are pinned by digest; the temporary historical MinIO readers are pinned by release and
architecture-specific SHA-256. Reader licenses and matching upstream source archives are retained
inside the image under `/usr/share/open-balena-s3/source`. Their original licensing still applies;
the switch to an Apache-2.0 server does not relicense bundled migration dependencies.

### Development and verification

Run the dependency-free unit suite:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Build the actual runtime image, then run the opt-in Docker integration tests:

```sh
docker build -t open-balena-s3:test .
OB_S3_TEST_IMAGE=open-balena-s3:test python3 tests/integration.py --run --registry
```

The integration harness uses disposable synthetic volumes and real historical FS/current
single-drive XL stores. It exercises upgrade, restart, retained-source cleanup, failure gating and
the real registry storage driver. It must never be pointed at an existing deployment's volume.
