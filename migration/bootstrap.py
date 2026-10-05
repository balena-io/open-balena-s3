"""Single-volume RustFS upgrade, gated serving, and restart-safe source cleanup."""

import contextlib
import ctypes
import datetime
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

from transfer import MigrationError, S3, S3Error, Transfer, xml

ROOT = Path("/export")
RESERVED = ".open-balena-storage"
FS_RELEASE = "RELEASE.2022-10-24T18-35-07Z"
XL_RELEASE = "RELEASE.2025-09-07T16-13-09Z"
SOURCE_ENDPOINT = "http://127.0.0.1:9002"
TARGET_ENDPOINT = "http://127.0.0.1:8333"
PUBLIC_ENDPOINT = "http://127.0.0.1:80"
STOP = threading.Event()


def boolean(value):
    if value.strip().lower() in ("1", "true"):
        return True
    if value.strip().lower() in ("0", "false"):
        return False
    raise MigrationError("CLEANUP_AFTER_MIGRATION must be 0, 1, false, or true")


def settings():
    key = os.environ.get("S3_MINIO_ACCESS_KEY") or os.environ.get("RUSTFS_ACCESS_KEY")
    secret = os.environ.get("S3_MINIO_SECRET_KEY") or os.environ.get("RUSTFS_SECRET_KEY")
    if not key or not secret:
        raise MigrationError("S3_MINIO_ACCESS_KEY and S3_MINIO_SECRET_KEY are required")
    region = (os.environ.get("S3_REGION") or os.environ.get("MINIO_SITE_REGION")
              or os.environ.get("MINIO_REGION_NAME") or os.environ.get("RUSTFS_REGION")
              or "us-east-1")
    return key, secret, region


def client(endpoint):
    return S3(endpoint, *settings()[:2], region=settings()[2], timeout=5)


def directory_sync(path):
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def volume_sync(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syncfs(fd) != 0:
            raise MigrationError("Could not durably flush the storage filesystem")
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_sync(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MigrationError("Invalid or unreadable storage record: " + Path(path).name) from None


def safe_directory(path, root, create=False):
    path, root = Path(path), Path(root)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise MigrationError("Storage directory escapes the mounted volume")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise MigrationError("Required storage directory is missing")
    if path.stat().st_dev != root.stat().st_dev:
        raise MigrationError("Storage directories must be on the same filesystem")
    return path


def detect_format(root):
    root = Path(root)
    if (root / ".rustfs.sys").exists():
        raise MigrationError("Untracked RustFS volume; refusing to initialize over existing data")
    format_path = root / ".minio.sys" / "format.json"
    if format_path.is_symlink():
        raise MigrationError("MinIO format record must not be a symbolic link")
    if not format_path.exists():
        entries = [p.name for p in root.iterdir() if p.name not in (RESERVED, "lost+found")]
        if entries:
            raise MigrationError("Nonempty volume has no recognized MinIO format record")
        return "fresh"
    value = read_json(format_path)
    if not isinstance(value, dict) or value.get("version") != "1":
        raise MigrationError("Unsupported MinIO format version")
    try:
        uuid.UUID(value["id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise MigrationError("Invalid MinIO deployment identity") from None
    if value.get("format") == "fs" and value.get("fs") == {"version": "2"}:
        return "fs"
    xl = value.get("xl")
    if (value.get("format") != "xl-single" or not isinstance(xl, dict)
            or xl.get("version") != "3" or xl.get("distributionAlgo") != "SIPMOD+PARITY"
            or not isinstance(xl.get("sets"), list) or len(xl["sets"]) != 1
            or not isinstance(xl["sets"][0], list) or len(xl["sets"][0]) != 1
            or xl.get("this") != xl["sets"][0][0]):
        raise MigrationError("Only verified single-drive XL-v3 and legacy FS-v2 volumes are supported")
    try:
        uuid.UUID(xl["this"])
    except (ValueError, TypeError, AttributeError):
        raise MigrationError("Invalid MinIO drive identity") from None
    return "xl-single"


def reject_custom_iam(source):
    iam = Path(source) / ".minio.sys" / "config" / "iam"
    for name in ("users", "service-accounts", "policydb"):
        directory = iam / name
        if directory.is_symlink():
            raise MigrationError("MinIO IAM directory must not be a symbolic link")
        if directory.exists() and any(p.is_file() or p.is_symlink() for p in directory.rglob("*")):
            raise MigrationError("Custom MinIO IAM identities/mappings require a separate migration")

def reject_mixed_fs_layout(source):
    for bucket in Path(source).iterdir():
        if bucket.name.startswith(".") or not bucket.is_dir():
            continue
        for metadata in bucket.rglob("xl.meta"):
            if metadata.is_file():
                with metadata.open("rb") as stream:
                    if stream.read(4) == b"XL2 ":
                        raise MigrationError("FS format record conflicts with XL object metadata; "
                                             "restore or separately recover the original layout")

def validate_tree(path, root, excluded=()):
    safe_directory(path, root)
    if sys.platform.startswith("linux"):
        try:
            mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError:
            raise MigrationError("Cannot inspect filesystem mounts before accessing the source") from None
        for line in mounts:
            fields = line.split()
            if len(fields) < 6:
                raise MigrationError("Invalid filesystem mount information")
            point = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
            if point != Path(root) and point.is_relative_to(Path(path)):
                raise MigrationError("Source tree contains another mounted filesystem")
    device = Path(root).stat().st_dev
    for current, directories, files in os.walk(path, followlinks=False):
        if Path(current) == Path(path):
            directories[:] = [name for name in directories if name not in excluded]
        for name in directories + files:
            entry = Path(current) / name
            if entry.is_symlink() or entry.stat().st_dev != device:
                raise MigrationError("Source tree contains a symbolic link or another mounted filesystem")


class Volume:
    def __init__(self, root=ROOT, *, create=True):
        self.root = Path(root)
        safe_directory(self.root, self.root)
        if create and not (self.root / RESERVED).exists():
            detect_format(self.root)
        self.home = safe_directory(self.root / RESERVED, self.root, create=create)
        if create:
            os.chmod(self.home, 0o700)
        self.path = self.home / "state.json"
        self.source = self.home / "minio-source"
        self.target = self.home / "rustfs"
        self.manifest = self.home / "manifest.json"
        self.state = None

    @contextlib.contextmanager
    def lock(self):
        import fcntl
        path = self.home / "lock"
        if path.is_symlink():
            raise MigrationError("Storage lock must not be a symbolic link")
        with path.open("a", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationError("Another upgrade process owns the mounted volume") from None
            yield

    def load(self, *, initialize=True):
        if self.path.is_symlink() or self.manifest.is_symlink():
            raise MigrationError("Migration records must not be symbolic links")
        if self.path.exists():
            state = read_json(self.path)
            if (not isinstance(state, dict) or state.get("schema") != 1
                    or state.get("mode") not in ("fresh", "fs", "xl-single")
                    or state.get("phase") not in ("staging", "capturing", "copying", "verifying", "complete")
                    or type(state.get("cleaned")) is not bool
                    or type(state.get("verified")) is not bool
                    or ("cleanup_started" in state and type(state["cleanup_started"]) is not bool)
                    or not isinstance(state.get("source_entries"), list)):
                raise MigrationError("Invalid storage state schema")
            try:
                if uuid.UUID(state["store_id"]).hex != state["store_id"]:
                    raise ValueError()
            except (KeyError, ValueError, TypeError, AttributeError):
                raise MigrationError("Invalid storage state identity") from None
            if state["phase"] == "complete" and state.get("verified") is not True:
                raise MigrationError("Completion record is not verified")
            if state["cleaned"] and (state["mode"] != "fs" or state["phase"] != "complete"):
                raise MigrationError("Cleanup record does not belong to a completed FS migration")
            if state["mode"] != "fs" and state["source_entries"]:
                raise MigrationError("Native/fresh state must not authorize source deletion")
            for name in state["source_entries"]:
                if (not isinstance(name, str) or name in ("", ".", "..", RESERVED, "lost+found")
                        or "/" in name or "\\" in name):
                    raise MigrationError("Invalid staged source entry")
            self.state = state
        else:
            if not initialize:
                raise MigrationError("Storage upgrade has no durable state record")
            if any(p.name != "lock" for p in self.home.iterdir()):
                raise MigrationError("Untracked migration files; refusing an automatic fresh start")
            mode = detect_format(self.root)
            entries = []
            if mode == "fs":
                entries = sorted(p.name for p in self.root.iterdir()
                                 if p.name not in (RESERVED, "lost+found"))
            self.state = {"schema": 1, "store_id": uuid.uuid4().hex, "mode": mode,
                          "phase": "staging" if mode == "fs" else "capturing",
                          "source_entries": entries, "cleaned": False, "verified": False}
            self.save()
        return self.state

    def save(self):
        atomic_json(self.path, self.state)

    @property
    def bucket(self):
        return "open-balena-storage-" + self.state["store_id"]

    def stage(self):
        if self.state["mode"] != "fs" or self.state["phase"] != "staging":
            return
        safe_directory(self.source, self.root, create=True)
        os.chmod(self.source, 0o700)
        inode = self.source.stat().st_ino
        if "source_inode" not in self.state:
            self.state["source_inode"] = inode
            self.save()
        elif self.state["source_inode"] != inode:
            raise MigrationError("Staged source directory was replaced")
        validate_tree(self.source, self.root)
        validate_tree(self.root, self.root, excluded=(RESERVED, "lost+found"))
        for name in self.state["source_entries"]:
            old, new = self.root / name, self.source / name
            if old.is_symlink() or new.is_symlink():
                raise MigrationError("Source entries must not be symbolic links")
            if old.exists() == new.exists():
                raise MigrationError("Staging collision or missing source entry")
            if old.exists():
                if old.stat().st_dev != self.root.stat().st_dev:
                    raise MigrationError("Source entry is on a different filesystem")
                os.rename(old, new)
                directory_sync(self.root)
                directory_sync(self.source)
        if detect_format(self.source) != "fs":
            raise MigrationError("Staged source is not the expected FS-v2 store")
        self.state["phase"] = "capturing"
        self.save()

    def check_capacity(self, size, *, copied_bytes=0, largest_object=0):
        if type(size) is not int or size < 0:
            raise MigrationError("Invalid source size estimate")
        # Copying preserves the source; leave room for indices and multipart work.
        if type(copied_bytes) is not int or copied_bytes < 0 or copied_bytes > size:
            raise MigrationError("Invalid copied size estimate")
        if type(largest_object) is not int or largest_object < 0 or largest_object > size:
            raise MigrationError("Invalid largest object size estimate")
        # Transfers are serial; replacement of an existing object may stage one payload.
        replacement = largest_object if copied_bytes else 0
        required = size - copied_bytes + replacement + max(64 * 1024 * 1024, size // 10)
        free = shutil.disk_usage(self.root).free
        if free < required:
            raise MigrationError("Insufficient free space: need %d bytes; available %d. "
                                 "Legacy FS migration requires approximately 2x storage plus overhead; "
                                 "resuming may also need temporary object-replacement space."
                                 % (required, free))

    def identity(self):
        return json.dumps({"schema": 1, "store_id": self.state["store_id"]},
                          sort_keys=True, separators=(",", ":")).encode()

    def validate_backend_files(self):
        root = self.target if self.state["mode"] == "fs" else self.root
        safe_directory(root, self.root)
        system = safe_directory(root / ".rustfs.sys", self.root)
        format_file = system / "format.json"
        if format_file.is_symlink() or not format_file.is_file():
            raise MigrationError("Completed RustFS backend metadata is missing")
        if not isinstance(read_json(format_file), dict):
            raise MigrationError("Completed RustFS backend metadata is invalid")

    def validate_probe(self, s3, allow_absent=False):
        try:
            buckets = xml(s3.request("GET", attempts=1)[2])
            found = [node.findtext("Name") for node in buckets.findall("./Buckets/Bucket")]
            if self.bucket not in found:
                if allow_absent:
                    return False
                raise MigrationError("RustFS storage identity bucket is missing")
            listing = xml(s3.request("GET", self.bucket, query={"list-type": "2"},
                                     attempts=1)[2])
            keys = [node.findtext("Key") for node in listing.findall("Contents")]
            if listing.findtext("IsTruncated", "false").lower() != "false":
                raise MigrationError("Unexpected objects in storage identity bucket")
            if not keys and allow_absent:
                return False
            if keys != ["identity.json"]:
                raise MigrationError("Unexpected objects in storage identity bucket")
            body = s3.request("GET", self.bucket, "identity.json", attempts=1, body_limit=4097)[2]
            if body != self.identity():
                raise MigrationError("RustFS backend belongs to a different storage identity")
            return True
        except S3Error:
            raise MigrationError("Could not authenticate the RustFS storage identity") from None

    def create_probe(self, s3):
        if self.validate_probe(s3, allow_absent=True):
            return
        existing = {node.findtext("Name") for node in xml(s3.request("GET")[2]).findall("./Buckets/Bucket")}
        if self.bucket not in existing:
            s3.request("PUT", self.bucket)
        s3.request("PUT", self.bucket, "identity.json", body=self.identity(),
                   headers={"content-type": "application/json"})
        self.validate_probe(s3)

    def cleanup(self, enabled):
        if not enabled or self.state["cleaned"] or self.state["mode"] != "fs":
            return
        if self.state["phase"] != "complete" or self.state.get("verified") is not True:
            raise MigrationError("Cleanup requires a verified durable completion record")
        if self.source.exists():
            safe_directory(self.source, self.root)
            if self.state.get("source_inode") != self.source.stat().st_ino:
                raise MigrationError("Cleanup source directory is not the original staged source")
            validate_tree(self.source, self.root)
            self.state["cleanup_started"] = True
            self.save()
            shutil.rmtree(self.source)
            directory_sync(self.home)
        elif not self.state.get("cleanup_started"):
            raise MigrationError("Retained source disappeared without an authorized cleanup")
        self.state["cleaned"] = True
        self.state["cleaned_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.save()
        print("Verified retained MinIO source cleaned up", flush=True)


class Child:
    def __init__(self, name, args, env):
        self.name = name
        logs = Path("/run/open-balena-storage")
        logs.mkdir(mode=0o700, exist_ok=True)
        self.log = (logs / (name + "-stdout.log")).open("wb")
        os.chmod(self.log.name, 0o600)
        self.process = subprocess.Popen(args, env=env, stdout=self.log, stderr=self.log,
                                        start_new_session=True)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.log.close()


def wait_ready(child, s3):
    deadline = time.monotonic() + 180
    while not STOP.is_set():
        if child.process.poll() is not None:
            raise MigrationError(child.name + " exited before readiness; private log in /run/open-balena-storage")
        try:
            if xml(s3.request("GET", attempts=1)[2]).tag == "ListAllMyBucketsResult":
                return
            raise MigrationError("Backend returned an invalid bucket-list response")
        except S3Error as error:
            if error.status in (401, 403):
                raise MigrationError(child.name + " authentication failed") from None
            if time.monotonic() >= deadline:
                raise MigrationError(child.name + " readiness deadline expired") from None
            STOP.wait(1)
        except (MigrationError, OSError):
            if time.monotonic() >= deadline:
                raise MigrationError(child.name + " readiness deadline expired") from None
            STOP.wait(1)
    raise MigrationError("Storage startup interrupted")


def start_reader(mode, root):
    key, secret, region = settings()
    env = dict(os.environ, MINIO_ROOT_USER=key, MINIO_ROOT_PASSWORD=secret,
               MINIO_BROWSER="off", MINIO_UPDATE="off",
               MINIO_REGION_NAME=region, MINIO_SITE_REGION=region)
    reader = "/usr/local/bin/minio-fs" if mode == "fs" else "/usr/local/bin/minio-xl"
    child = Child("minio-reader", [reader, "--quiet", "server", "--address", "127.0.0.1:9002",
                                    "--console-address", "127.0.0.1:9003", str(root)], env)
    try:
        wait_ready(child, client(SOURCE_ENDPOINT))
        return child
    except BaseException:
        child.stop()
        raise


def start_rustfs(root):
    key, secret, region = settings()
    rolling_logs = Path("/run/open-balena-storage/rustfs-internal")
    rolling_logs.mkdir(parents=True, mode=0o700, exist_ok=True)
    env = dict(os.environ, RUSTFS_ACCESS_KEY=key, RUSTFS_SECRET_KEY=secret,
               RUSTFS_VOLUMES=str(root), RUSTFS_ADDRESS="127.0.0.1:8333",
               RUSTFS_CONSOLE_ENABLE="false", RUSTFS_OBS_LOG_DIRECTORY=str(rolling_logs),
               RUSTFS_OBS_LOGGER_LEVEL="warn", RUSTFS_REGION=region)
    child = Child("rustfs", ["/usr/local/bin/rustfs", str(root)], env)
    try:
        wait_ready(child, client(TARGET_ENDPOINT))
        return child
    except BaseException:
        child.stop()
        raise


def bucket_names(value):
    names = [name.strip() for name in value.split(";") if name.strip()]
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name) for name in names):
        raise MigrationError("BUCKETS must contain valid semicolon-separated S3 bucket names")
    return list(dict.fromkeys(names))


def ensure_buckets(s3, value):
    names = bucket_names(value)
    existing = {node.findtext("Name") for node in xml(s3.request("GET")[2]).findall("./Buckets/Bucket")}
    for name in names:
        if name not in existing:
            s3.request("PUT", name)
            print("Created configured bucket: " + name, flush=True)

def manifest_stats(manifest):
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or not isinstance(manifest.get("buckets"), dict)):
        raise MigrationError("Invalid saved source manifest")
    total = largest = 0
    for entries in manifest["buckets"].values():
        if not isinstance(entries, dict):
            raise MigrationError("Invalid saved source inventory")
        for record in entries.values():
            if not isinstance(record, dict) or type(record.get("size")) is not int or record["size"] < 0:
                raise MigrationError("Invalid saved source object size")
            total += record["size"]
            largest = max(largest, record["size"])
    return total, largest


def manifest_size(manifest):
    return manifest_stats(manifest)[0]


def existing_copy_bytes(s3, manifest, control_bucket):
    """Credit live, size-matching destination objects when resuming a partial copy."""
    # The transfer verifier still independently checks all content and metadata.
    total = 0
    buckets = xml(s3.request("GET")[2])
    found = {node.findtext("Name") for node in buckets.findall("./Buckets/Bucket")}
    for bucket in manifest["buckets"]:
        if bucket not in found or bucket == control_bucket:
            continue
        query, seen = {"list-type": "2", "encoding-type": "url"}, set()
        while True:
            listing = xml(s3.request("GET", bucket, query=query)[2])
            if listing.tag != "ListBucketResult":
                raise MigrationError("Invalid destination listing during space estimation")
            for node in listing.findall("Contents"):
                key = urllib.parse.unquote(node.findtext("Key") or "")
                try:
                    size = int(node.findtext("Size"))
                except (TypeError, ValueError):
                    raise MigrationError("Invalid destination size during space estimation") from None
                if size < 0:
                    raise MigrationError("Invalid destination size during space estimation")
                expected = manifest["buckets"][bucket].get(key)
                if expected is not None and expected["size"] == size:
                    total += size
            if listing.findtext("IsTruncated", "false").lower() != "true":
                break
            token = listing.findtext("NextContinuationToken")
            if not token or token in seen:
                raise MigrationError("Invalid destination pagination during space estimation")
            seen.add(token)
            query["continuation-token"] = token
    return min(total, manifest_size(manifest))


class Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(("127.0.0.1", 8333), timeout=10) as backend:
                backend.settimeout(60)
                self.request.settimeout(60)
                with selectors.DefaultSelector() as events:
                    events.register(self.request, selectors.EVENT_READ, backend)
                    events.register(backend, selectors.EVENT_READ, self.request)
                    while events.get_map() and not STOP.is_set():
                        for event, _ in events.select(timeout=1):
                            data = event.fileobj.recv(65536)
                            if data:
                                event.data.sendall(data)
                            else:
                                events.unregister(event.fileobj)
                                event.data.shutdown(socket.SHUT_WR)
        except (OSError, TimeoutError) as error:
            print("S3 relay connection ended: " + type(error).__name__, file=sys.stderr, flush=True)


def serve():
    enabled = boolean(os.environ.get("CLEANUP_AFTER_MIGRATION", "false"))
    settings()
    bucket_names(os.environ.get("BUCKETS", ""))
    volume = Volume()
    rustfs = reader = relay = None
    def terminate(_signum, _frame):
        STOP.set()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        with volume.lock():
            state = volume.load()
            volume.stage()
            mode = state["mode"]
            root = volume.target if mode == "fs" else volume.root
            if state["phase"] != "complete" and mode != "fresh":
                source_root = volume.source if mode == "fs" else volume.root
                safe_directory(source_root, volume.root)
                validate_tree(source_root, volume.root, excluded=(RESERVED, "lost+found"))
                reject_custom_iam(source_root)
                if mode == "fs":
                    reject_mixed_fs_layout(source_root)
                if not volume.manifest.exists():
                    if detect_format(source_root) != mode:
                        raise MigrationError("Source format changed during migration")
                    reader = start_reader(mode, source_root)
                    transfer = Transfer(client(SOURCE_ENDPOINT), client(TARGET_ENDPOINT),
                                        source_format=mode,
                                        source_release=FS_RELEASE if mode == "fs" else XL_RELEASE,
                                        target_release="1.0.1")
                    print("Capturing and validating source inventory", flush=True)
                    manifest = transfer.capture_manifest()
                    atomic_json(volume.manifest, manifest)
                    state["phase"] = "copying" if mode == "fs" else "verifying"
                    volume.save()
                    if mode != "fs":
                        reader.stop()
                        reader = None
                elif mode == "fs":
                    reader = start_reader(mode, source_root)
                if mode == "fs":
                    manifest = read_json(volume.manifest)
                    size, largest = manifest_stats(manifest)
                    if not root.exists():
                        volume.check_capacity(size)
            safe_directory(root, volume.root, create=state["phase"] != "complete")
            if state["phase"] == "complete":
                volume.validate_backend_files()
            rustfs = start_rustfs(root)
            target = client(TARGET_ENDPOINT)
            if state["phase"] != "complete":
                volume.validate_probe(target, allow_absent=True)
                if mode == "fs":
                    volume.check_capacity(size, copied_bytes=existing_copy_bytes(
                        target, manifest, volume.bucket), largest_object=largest)
                    transfer = Transfer(client(SOURCE_ENDPOINT), target,
                                        source_format="fs", source_release=FS_RELEASE,
                                        allowed_extra_buckets=(volume.bucket,), target_release="1.0.1")
                    transfer.run()
                    report = transfer.verify_manifest(manifest, allowed_extra_buckets=(volume.bucket,))
                    reader.stop()
                    reader = None
                elif mode == "xl-single":
                    transfer = Transfer(client(SOURCE_ENDPOINT), target,
                                        source_format=mode, source_release=XL_RELEASE,
                                        target_release="1.0.1")
                    report = transfer.verify_manifest(read_json(volume.manifest),
                                                      allowed_extra_buckets=(volume.bucket,))
                else:
                    report = {"buckets": 0, "objects": 0, "bytes": 0}
                volume.create_probe(target)
                if STOP.is_set():
                    raise MigrationError("Storage startup interrupted before completion")
                volume_sync(volume.root)
                state.update(phase="complete", verified=True, report=report,
                             completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                volume.save()
                print("RustFS storage verified; upgrade complete", flush=True)
            volume.validate_probe(target)
            ensure_buckets(target, os.environ.get("BUCKETS", ""))
            volume.cleanup(enabled)
            relay = Relay(("0.0.0.0", 80), RelayHandler)
            thread = threading.Thread(target=relay.serve_forever, daemon=True)
            thread.start()
            print("RustFS ready on port 80 (mode: " + mode + ")", flush=True)
            while not STOP.wait(1):
                if rustfs.process.poll() is not None or not thread.is_alive():
                    raise MigrationError("RustFS or public relay stopped unexpectedly")
    finally:
        STOP.set()
        if relay:
            relay.shutdown()
            relay.server_close()
        if reader:
            reader.stop()
        if rustfs:
            rustfs.stop()


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve":
        serve()
    elif command == "health":
        volume = Volume(create=False)
        state = volume.load(initialize=False)
        if state["phase"] != "complete":
            raise MigrationError("Storage upgrade is not complete")
        volume.validate_backend_files()
        volume.validate_probe(client(PUBLIC_ENDPOINT))
    elif command == "buckets":
        ensure_buckets(client(PUBLIC_ENDPOINT), os.environ.get("BUCKETS", ""))
    else:
        raise MigrationError("Unknown storage command")


if __name__ == "__main__":
    try:
        main()
    except (MigrationError, OSError) as error:
        print("Storage service failed: " + str(error), file=sys.stderr, flush=True)
        sys.exit(1)
