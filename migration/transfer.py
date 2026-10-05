"""Fail-closed S3 transfer, using only Python's stdlib and rclone.

The caller must fence source writers throughout preflight/run and make the
destination durable before recording completion. This module writes no markers,
never deletes buckets or live objects, and does not migrate server/account IAM
identities. Failed metadata-repair multipart uploads are explicitly aborted.
"""

import base64
import datetime
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class MigrationError(Exception):
    """A migration could not be proven safe."""


LEGACY_FS_RELEASE = "RELEASE.2022-10-24T18-35-07Z"
MODERN_XL_RELEASE = "RELEASE.2025-09-07T16-13-09Z"


class S3Error(MigrationError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        # Response text/code can contain credentials. Keep it internal.
        super().__init__("S3 request failed (HTTP %d)" % status)


def xml(body, expected=None):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise MigrationError("Invalid S3 XML response") from None
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if expected and root.tag != expected:
        raise MigrationError("Unexpected S3 XML response")
    return root


def validate_endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        if (parsed.scheme != "http" or not address.is_loopback
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or port == 0):
            raise ValueError()
    except (ValueError, TypeError):
        raise MigrationError("S3 endpoint must be a literal loopback HTTP origin") from None
    host = "[" + str(address) + "]" if address.version == 6 else str(address)
    return "http://" + host + (":" + str(port) if port else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class S3:
    """Path-style S3 client with SigV4; redirects and environment proxies disabled."""

    def __init__(self, endpoint, key, secret, region="us-east-1", timeout=30):
        self.endpoint = validate_endpoint(endpoint)
        self.key, self.secret, self.region, self.timeout = key, secret, region, timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, method, bucket="", key="", query=None, body=b"", headers=None,
                stream=False, attempts=3, body_limit=None):
        if body_limit is not None and (type(body_limit) is not int or body_limit < 0):
            raise MigrationError("Invalid S3 response body limit")
        path = "/" + urllib.parse.quote(bucket, safe="")
        if key:
            path += "/" + urllib.parse.quote(key, safe="/~")
        pairs = sorted((urllib.parse.quote(str(k), safe="~"),
                        urllib.parse.quote(str(v), safe="~"))
                       for k, v in (query or {}).items())
        query_string = "&".join(k + "=" + v for k, v in pairs)
        url = self.endpoint + path + ("?" + query_string if query_string else "")
        headers = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
        now = datetime.datetime.now(datetime.timezone.utc)
        date, stamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers.update({"host": urllib.parse.urlsplit(self.endpoint).netloc,
                        "x-amz-date": stamp, "x-amz-content-sha256": payload_hash})
        names = ";".join(sorted(headers))
        canonical_headers = "".join(
            k + ":" + " ".join(headers[k].split()) + "\n" for k in sorted(headers))
        canonical = "\n".join((method, path, query_string, canonical_headers,
                               names, payload_hash))
        scope = date + "/" + self.region + "/s3/aws4_request"
        signing_key = ("AWS4" + self.secret).encode()
        for part in (date, self.region, "s3", "aws4_request"):
            signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
        string = "AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + hashlib.sha256(
            canonical.encode()).hexdigest()
        signature = hmac.new(signing_key, string.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = ("AWS4-HMAC-SHA256 Credential=" + self.key + "/" + scope
                                    + ", SignedHeaders=" + names + ", Signature=" + signature)
        request = urllib.request.Request(
            url, data=body if method in ("PUT", "POST") else None,
            method=method, headers=headers)
        for attempt in range(attempts):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    if stream:
                        checksum, size = hashlib.sha256(), 0
                        deadline = time.monotonic() + 86400
                        while True:
                            chunk = response.read(1024 * 1024)
                            if time.monotonic() > deadline:
                                raise MigrationError("S3 object read deadline exceeded")
                            if not chunk:
                                break
                            checksum.update(chunk)
                            size += len(chunk)
                        content = {"size": size, "sha256": checksum.hexdigest()}
                    else:
                        if body_limit is None:
                            content = response.read()
                        else:
                            content = response.read(body_limit + 1)
                            if len(content) > body_limit:
                                raise MigrationError("S3 response body exceeds limit")
                    return response.status, dict(response.headers), content
            except urllib.error.HTTPError as error:
                with error:
                    content = error.read(65536)
                try:
                    code = xml(content).findtext("Code") or "Unknown"
                except MigrationError:
                    code = "NonXMLResponse"
                if error.code >= 500 and attempt < attempts - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise S3Error(error.code, code) from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                if attempt == attempts - 1:
                    raise MigrationError("S3 connection failed or timed out") from None
                time.sleep(2 ** attempt)
        raise MigrationError("S3 request attempts exhausted")

    def tags(self, bucket, key):
        root = xml(self.request("GET", bucket, key, {"tagging": ""})[2], "Tagging")
        if len(root) != 1 or root[0].tag != "TagSet":
            raise MigrationError("Incomplete S3 tag response")
        tags = []
        for node in root.findall("./TagSet/Tag"):
            name, value = node.findtext("Key"), node.findtext("Value")
            if not name or value is None or name in dict(tags):
                raise MigrationError("Invalid S3 tags")
            tags.append((name, value))
        return sorted(tags)

    def put_tags(self, bucket, key, tags):
        root = ET.Element("Tagging", xmlns="http://s3.amazonaws.com/doc/2006-03-01/")
        tagset = ET.SubElement(root, "TagSet")
        for name, value in tags:
            node = ET.SubElement(tagset, "Tag")
            ET.SubElement(node, "Key").text = name
            ET.SubElement(node, "Value").text = value
        body = ET.tostring(root)
        self.request("PUT", bucket, key, {"tagging": ""}, body,
                     {"content-type": "application/xml",
                      "content-md5": base64.b64encode(
                          hashlib.md5(body).digest()).decode()})


ABSENT_CODES = {
    "policy": {"NoSuchBucketPolicy"},
    "lifecycle": {"NoSuchLifecycleConfiguration"},
    "cors": {"NoSuchCORSConfiguration"},
    "replication": {"ReplicationConfigurationNotFoundError", "NoSuchReplicationConfiguration"},
    "encryption": {"ServerSideEncryptionConfigurationNotFoundError",
                   "NoSuchServerSideEncryptionConfiguration"},
    "object-lock": {"ObjectLockConfigurationNotFoundError"},
    "tagging": {"NoSuchTagSet"},
    "website": {"NoSuchWebsiteConfiguration"},
    "ownershipControls": {"OwnershipControlsNotFoundError"},
}

BUCKET_ROOTS = {
    "versioning": "VersioningConfiguration", "object-lock": "ObjectLockConfiguration",
    "lifecycle": "LifecycleConfiguration", "cors": "CORSConfiguration",
    "notification": "NotificationConfiguration", "replication": "ReplicationConfiguration",
    "encryption": "ServerSideEncryptionConfiguration", "tagging": "Tagging",
    "website": "WebsiteConfiguration", "logging": "BucketLoggingStatus",
    "requestPayment": "RequestPaymentConfiguration",
    "accelerate": "AccelerateConfiguration", "ownershipControls": "OwnershipControls",
}


def _synthetic_private_acl(root):
    """Recognize only the audited pinned readers' synthetic private ACL response."""
    def children(node, names):
        return (node is not None and [child.tag for child in node] == names
                and not (node.text or "").strip())

    if root.attrib or not children(root, ["Owner", "AccessControlList"]):
        return False
    owner, access = root
    if owner.attrib or not children(owner, ["ID", "DisplayName"]):
        return False
    if any(node.attrib or list(node) or (node.text or "").strip() for node in owner):
        return False
    if access.attrib or not children(access, ["Grant"]):
        return False
    grant = access[0]
    if grant.attrib or not children(grant, ["Grantee", "Permission"]):
        return False
    grantee, permission = grant
    if (grantee.attrib != {
            "{http://www.w3.org/2001/XMLSchema-instance}type": "CanonicalUser"}
            or not children(grantee, ["Type"])
            or grantee[0].attrib or list(grantee[0])
            or grantee[0].text != "CanonicalUser"
            or permission.attrib or list(permission) or permission.text != "FULL_CONTROL"):
        return False
    return True


def _rustfs_default_acl(body):
    """Recognize the fixed canned ACL emitted by managed RustFS 1.0.1."""
    root = ET.fromstring(body)  # Already validated by xml() at the call site.
    namespace = "{http://s3.amazonaws.com/doc/2006-03-01/}"

    def children(node, names):
        return (node is not None and not node.attrib and not (node.text or "").strip()
                and len(node) == len(names)
                and {child.tag for child in node} == {namespace + name for name in names}
                and all(not (child.tail or "").strip() for child in node))

    if root.tag != namespace + "AccessControlPolicy" or not children(
            root, ["Owner", "AccessControlList"]):
        return False
    owner = root.find(namespace + "Owner")
    access = root.find(namespace + "AccessControlList")
    if not children(owner, ["ID", "DisplayName"]) or not children(access, ["Grant"]):
        return False
    # Public fixed owner constants from RustFS 1.0.1 s3_api/common.rs, not credentials.
    expected_owner = {
        "ID": "c19050dbcee97fda828689dda99097a6321af2248fa760517237346e5d9c8a66",
        "DisplayName": "rustfs",
    }
    if any(node.attrib or list(node) or node.text != expected_owner[node.tag[len(namespace):]]
           for node in owner):
        return False
    grant = access[0]
    if not children(grant, ["Grantee", "Permission"]):
        return False
    grantee = grant.find(namespace + "Grantee")
    permission = grant.find(namespace + "Permission")
    return (grantee.attrib == {
        "{http://www.w3.org/2001/XMLSchema-instance}type": "CanonicalUser"}
        and not list(grantee) and not (grantee.text or "").strip()
        and not permission.attrib and not list(permission) and permission.text == "FULL_CONTROL")


def private_acl(client, bucket, key="", *, trusted_reader=False, target_release=None):
    body = client.request("GET", bucket, key, {"acl": ""})[2]
    root = xml(body, "AccessControlPolicy")
    if trusted_reader and _synthetic_private_acl(root):
        return
    if target_release == "1.0.1" and _rustfs_default_acl(body):
        return
    owner = root.findtext("./Owner/ID")
    grants = root.findall("./AccessControlList/Grant")
    if not owner or not grants or any(
            grant.findtext("./Grantee/ID") != owner
            or grant.findtext("Permission") != "FULL_CONTROL"
            or grant.find("./Grantee/URI") is not None
            or grant.find("./Grantee/EmailAddress") is not None for grant in grants):
        raise MigrationError("Unsupported non-private S3 ACL")


def guard_bucket(client, bucket, *, legacy_fs=False, trusted_reader=False, target_release=None,
                 inspect_uploads=True):
    versioning_empty = False
    for feature in (*BUCKET_ROOTS, "policy"):
        try:
            body = client.request("GET", bucket, query={feature: ""})[2]
        except S3Error as error:
            if (target_release == "1.0.1" and feature == "encryption" and error.status == 400
                    and error.code == "ServerSideEncryptionConfigurationNotFoundError"):
                continue
            if (target_release == "1.0.1" and feature == "ownershipControls"
                    and error.status == 501 and error.code == "NotImplemented"):
                continue
            if (trusted_reader and feature == "ownershipControls" and error.status == 501
                    and error.code == "NotImplemented"):
                continue
            if error.status == 404 and error.code in ABSENT_CODES.get(feature, set()):
                continue
            raise MigrationError("Cannot inspect bucket " + feature) from None
        if feature == "policy":
            try:
                policy = json.loads(body)
            except (ValueError, TypeError):
                raise MigrationError("Invalid bucket policy response") from None
            if policy not in ({}, None):
                raise MigrationError("Unsupported bucket policy")
            continue
        root = xml(body, BUCKET_ROOTS[feature])
        if feature == "versioning":
            versioning_empty = (not root.attrib and not list(root)
                                and not (root.text or "").strip()) or (
                legacy_fs and len(root) == 1 and root[0].tag == "Status"
                and not root.attrib and not root[0].attrib and not list(root[0])
                and not (root[0].text or "").strip() and not (root.text or "").strip())
            if versioning_empty:
                continue
        if feature == "requestPayment" and len(root) == 1 and root.findtext("Payer") == "BucketOwner":
            continue
        # Empty TagSet is the sole supported nested empty configuration.
        if feature == "tagging" and len(root) == 1 and root[0].tag == "TagSet":
            root = root[0]
        if list(root) or (root.text or "").strip():
            raise MigrationError("Unsupported bucket " + feature)
    private_acl(client, bucket, trusted_reader=trusted_reader, target_release=target_release)
    if inspect_uploads:
        uploads = xml(client.request("GET", bucket, query={"uploads": ""})[2],
                      "ListMultipartUploadsResult")
        if uploads.findall("Upload") or truncated(uploads):
            raise MigrationError("Unsupported pending multipart uploads")
    query, seen = {"versions": "", "encoding-type": "url"}, set()
    while True:
        try:
            body = client.request("GET", bucket, query=query)[2]
        except S3Error as error:
            if (legacy_fs and versioning_empty and not seen and error.status == 501
                    and error.code == "NotImplemented"):
                break
            raise
        root = xml(body, "ListVersionsResult")
        if root.findall("DeleteMarker") or any(
                node.findtext("VersionId") != "null" or node.findtext("IsLatest") != "true"
                for node in root.findall("Version")):
            raise MigrationError("Unsupported object version history")
        if not truncated(root):
            break
        marker = (root.findtext("NextKeyMarker"), root.findtext("NextVersionIdMarker"))
        if not marker[0] or marker in seen:
            raise MigrationError("Invalid version pagination")
        seen.add(marker)
        query.update({"key-marker": decode_key(root, marker[0]),
                      "version-id-marker": marker[1] or ""})


def truncated(root):
    value = root.findtext("IsTruncated")
    if value not in ("true", "false"):
        raise MigrationError("Missing or invalid pagination status")
    return value == "true"


def decode_key(root, value):
    encoding = root.findtext("EncodingType")
    if encoding not in (None, "url"):
        raise MigrationError("Unsupported listing encoding")
    if encoding == "url":
        try:
            if re.search(r"%(?![0-9a-fA-F]{2})", value):
                raise ValueError()
            return urllib.parse.unquote(value, errors="strict")
        except (ValueError, UnicodeError):
            raise MigrationError("Invalid encoded S3 key") from None
    return value


def listed_objects(client, bucket):
    result, query, seen = {}, {"list-type": "2", "encoding-type": "url"}, set()
    while True:
        root = xml(client.request("GET", bucket, query=query)[2], "ListBucketResult")
        if root.findall("CommonPrefixes"):
            raise MigrationError("Incomplete delimited object listing")
        for node in root.findall("Contents"):
            key = node.findtext("Key")
            try:
                size = int(node.findtext("Size"))
            except (ValueError, TypeError):
                raise MigrationError("Invalid object size") from None
            if key is None or size < 0:
                raise MigrationError("Invalid object listing")
            key = decode_key(root, key)
            if not key or key in result:
                raise MigrationError("Duplicate or invalid object key")
            result[key] = size
        if not truncated(root):
            return result
        token = root.findtext("NextContinuationToken")
        if not token or token in seen:
            raise MigrationError("Invalid object pagination")
        seen.add(token)
        query["continuation-token"] = token


def listed_buckets(client, *, ignore_buckets=()):
    root = xml(client.request("GET")[2], "ListAllMyBucketsResult")
    if root.find("Buckets") is None:
        raise MigrationError("Incomplete bucket listing")
    result = set()
    for node in root.findall("./Buckets/Bucket"):
        name = node.findtext("Name")
        if name in ignore_buckets:
            continue
        if (not name or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name)
                or name in result):
            raise MigrationError("Invalid bucket listing")
        result.add(name)
    return result


def allowed_buckets(values):
    if not isinstance(values, (tuple, list, set, frozenset)):
        raise MigrationError("Allowed extra buckets must be an explicit collection")
    if any(not isinstance(name, str)
           or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name) for name in values):
        raise MigrationError("Invalid allowed extra bucket")
    return frozenset(values)


def object_metadata(client, bucket, key, expected_size, with_identity=False):
    headers = {k.lower(): v for k, v in client.request("HEAD", bucket, key)[1].items()}
    try:
        size = int(headers.get("content-length", ""))
    except ValueError:
        raise MigrationError("Missing object size") from None
    if size != expected_size:
        raise MigrationError("Object listing and HEAD sizes differ")
    if any(name.startswith(("x-amz-server-side-encryption", "x-amz-object-lock"))
           for name in headers) or headers.get("x-amz-version-id", "null") != "null":
        raise MigrationError("Unsupported object encryption, retention or version")
    if (headers.get("x-amz-storage-class", "STANDARD") != "STANDARD"
            or headers.get("x-amz-website-redirect-location")
            or headers.get("x-amz-delete-marker", "false") != "false"
            or "x-amz-restore" in headers):
        raise MigrationError("Unsupported object storage semantics")
    fields = {"content-type", "cache-control", "content-disposition", "content-encoding",
              "content-language", "expires"}
    metadata = {name: value for name, value in headers.items()
                if name in fields or name.startswith("x-amz-meta-")}
    if with_identity:
        if not headers.get("etag") or not headers.get("last-modified"):
            raise MigrationError("Missing source object consistency headers")
        return metadata, {"etag": headers["etag"], "last-modified": headers["last-modified"]}
    return metadata


class Transfer:
    """Idempotent, non-destructive transfer with independently verified SHA-256.

    Copy uses rclone --transfers=1 so only one object payload is uploaded at once.
    Two concurrent 16 MiB upload parts belong to that same object, not separate
    object transfers; backend metadata, retries, and temporary files need margin.
    """

    def __init__(self, source: S3, target: S3, rclone_bin="/usr/local/bin/rclone", *,
                 source_format=None, source_release=None, allowed_extra_buckets=(),
                 target_release=None):
        """Proof parameters are assertions from the caller's offline audit.

        Validated FS storage with the bundled 2022 release permits three audited
        exceptions. Validated XL storage with the pinned 2025 release permits only
        synthetic private ACL and ownership-controls exceptions, never history.
        The managed RustFS 1.0.1 target permits its exact fixed canned ACL and
        audited encryption-absence HTTP 400 and ownership-controls HTTP 501
        responses; source handling and all other responses remain strict.
        IAM/offline validation, fencing, and ownership validation of any explicitly
        excluded control buckets remain the caller's responsibility.
        """
        validate_endpoint(source.endpoint)
        validate_endpoint(target.endpoint)
        if source.endpoint == target.endpoint:
            raise MigrationError("Source and destination must be distinct")
        self.source, self.target, self.rclone_bin = source, target, rclone_bin
        self._legacy_fs = source_format == "fs" and source_release == LEGACY_FS_RELEASE
        self._trusted_reader = self._legacy_fs or (
            source_format in ("xl", "xl-single") and source_release == MODERN_XL_RELEASE)
        self.allowed_extra_buckets = allowed_buckets(allowed_extra_buckets)
        self.target_release = target_release
        self.subprocess_timeout = 172800
        # Do not inherit ambient rclone flags, filters or credentials.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.upper().startswith(("RCLONE_", "AWS_"))}
        self.env["RCLONE_CONFIG"] = os.devnull
        for remote, client in (("SRC", source), ("DST", target)):
            for name, value in {
                    "TYPE": "s3", "PROVIDER": "Other", "ACCESS_KEY_ID": client.key,
                    "SECRET_ACCESS_KEY": client.secret, "ENDPOINT": client.endpoint,
                    "REGION": client.region, "FORCE_PATH_STYLE": "true", "ENV_AUTH": "false",
                    "DISABLE_CHECKSUM": "false"}.items():
                self.env["RCLONE_CONFIG_" + remote + "_" + name] = value
        self.env["NO_PROXY"] = "127.0.0.1,::1"
        for name in tuple(self.env):
            if name.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                del self.env[name]

    def _rclone(self, *args):
        try:
            result = subprocess.run([self.rclone_bin, *args], env=self.env,
                                    capture_output=True, timeout=self.subprocess_timeout,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise MigrationError("rclone execution failed or timed out") from None
        if result.returncode:
            raise MigrationError("rclone operation failed (exit %d)" % result.returncode)
        return result.stdout

    def _inventory(self, remote, bucket):
        try:
            entries = json.loads(self._rclone("lsjson", remote + ":" + bucket,
                                             "--recursive", "--metadata"))
        except (ValueError, UnicodeError):
            raise MigrationError("Invalid rclone inventory JSON") from None
        if not isinstance(entries, list):
            raise MigrationError("Invalid rclone inventory")
        result = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise MigrationError("Invalid rclone inventory entry")
            if entry.get("IsDir") is True:
                continue
            key, size = entry.get("Path"), entry.get("Size")
            if (entry.get("IsDir") is not False or not isinstance(key, str) or not key
                    or type(size) is not int or size < 0 or key in result):
                raise MigrationError("Invalid or duplicate rclone object")
            result[key] = size
        return result

    def _source_snapshot(self, *, use_rclone=True):
        snapshot = {}
        buckets = listed_buckets(self.source)
        for bucket in sorted(buckets):
            guard_bucket(self.source, bucket, legacy_fs=self._legacy_fs,
                         trusted_reader=self._trusted_reader)
            objects = listed_objects(self.source, bucket)
            if use_rclone and objects != self._inventory("src", bucket):
                raise MigrationError("Source keys omitted or changed by rclone (directory markers)")
            records = {}
            for key, size in objects.items():
                private_acl(self.source, bucket, key, trusted_reader=self._trusted_reader)
                metadata, identity = object_metadata(
                    self.source, bucket, key, size, with_identity=True)
                records[key] = {"size": size,
                                "metadata": metadata, "identity": identity,
                                "tags": self.source.tags(bucket, key)}
            snapshot[bucket] = records
        if listed_buckets(self.source) != buckets:
            raise MigrationError("Source buckets changed during inspection")
        return snapshot

    def preflight(self):
        """Inspect every live source object without writing the destination."""
        snapshot = self._source_snapshot()
        return sum(record["size"] for objects in snapshot.values() for record in objects.values())

    def capture_manifest(self):
        """Read source only, without rclone or writes, for sequential native reuse.

        Returns schema 1: ``{"schema": 1, "buckets": {bucket: {key: {
        "size": int, "metadata": {str: str}, "tags": [[str, str], ...],
        "sha256": hex_sha256}}}}``. The caller atomically persists this document,
        stops the source reader, then starts the destination before verification.
        This manifest contains no client credentials or endpoint information.
        """
        snapshot = self._source_snapshot(use_rclone=False)
        manifest = {"schema": 1, "buckets": {}}
        for bucket, records in snapshot.items():
            manifest["buckets"][bucket] = {}
            for key, record in records.items():
                content = self.source.request("GET", bucket, key, stream=True)[2]
                if content["size"] != record["size"]:
                    raise MigrationError("Source streamed size differs")
                manifest["buckets"][bucket][key] = {
                    "size": record["size"], "metadata": dict(record["metadata"]),
                    "tags": [list(tag) for tag in record["tags"]],
                    "sha256": content["sha256"]}
        if self._source_snapshot(use_rclone=False) != snapshot:
            raise MigrationError("Source changed during manifest capture")
        self._validate_manifest(manifest)
        return manifest

    @staticmethod
    def _validate_manifest(manifest):
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "buckets"}
                or type(manifest["schema"]) is not int or manifest["schema"] != 1
                or not isinstance(manifest["buckets"], dict)):
            raise MigrationError("Invalid migration manifest")
        for bucket, records in manifest["buckets"].items():
            if (not isinstance(bucket, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
                    or not isinstance(records, dict)):
                raise MigrationError("Invalid manifest bucket")
            for key, record in records.items():
                if (not isinstance(key, str) or not key or not isinstance(record, dict)
                        or set(record) != {"size", "metadata", "tags", "sha256"}
                        or type(record["size"]) is not int or record["size"] < 0
                        or not isinstance(record["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                        or not isinstance(record["metadata"], dict)
                        or any(not isinstance(name, str) or not isinstance(value, str)
                               for name, value in record["metadata"].items())
                        or not isinstance(record["tags"], list)):
                    raise MigrationError("Invalid manifest object")
                tags = record["tags"]
                if (any(not isinstance(tag, list) or len(tag) != 2
                        or any(not isinstance(value, str) for value in tag)
                        or not tag[0] for tag in tags)
                        or len({tag[0] for tag in tags}) != len(tags)
                        or tags != sorted(tags)):
                    raise MigrationError("Invalid manifest tags")

    @staticmethod
    def _manifest_report(manifest):
        records = manifest["buckets"]
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return {"buckets": len(records), "objects": sum(len(items) for items in records.values()),
                "bytes": sum(record["size"] for items in records.values()
                             for record in items.values()),
                "inventory_sha256": hashlib.sha256(encoded).hexdigest()}

    def verify_manifest(self, manifest, *, allowed_extra_buckets=()):
        """Read target only and verify a captured manifest, with no rclone/writes.

        Only explicitly allowed control buckets are excluded, after the caller
        independently establishes ownership and contents. All other extra/missing
        buckets and objects fail. The caller retains ownership of fsync/completion
        markers; a successful report alone is not durable state.
        """
        self._validate_manifest(manifest)
        expected_buckets = set(manifest["buckets"])
        allowed = allowed_buckets(allowed_extra_buckets)
        if allowed & expected_buckets:
            raise MigrationError("Cannot exclude source buckets")
        if listed_buckets(self.target, ignore_buckets=allowed) != expected_buckets:
            raise MigrationError("Target bucket inventory differs")
        for bucket, records in manifest["buckets"].items():
            guard_bucket(self.target, bucket, target_release=self.target_release)
            expected_objects = {key: record["size"] for key, record in records.items()}
            if listed_objects(self.target, bucket) != expected_objects:
                raise MigrationError("Target object inventory differs")
            identities = {}
            for key, record in records.items():
                private_acl(self.target, bucket, key, target_release=self.target_release)
                metadata, identity = object_metadata(
                    self.target, bucket, key, record["size"], with_identity=True)
                if metadata != record["metadata"]:
                    raise MigrationError("Target object metadata differs")
                if [list(tag) for tag in self.target.tags(bucket, key)] != record["tags"]:
                    raise MigrationError("Target object tags differ")
                content = self.target.request("GET", bucket, key, stream=True)[2]
                if content != {"size": record["size"], "sha256": record["sha256"]}:
                    raise MigrationError("Object content SHA-256 differs")
                identities[key] = identity
            if listed_objects(self.target, bucket) != expected_objects:
                raise MigrationError("Target inventory changed during verification")
            for key, record in records.items():
                metadata, identity = object_metadata(
                    self.target, bucket, key, record["size"], with_identity=True)
                if (identity != identities[key] or metadata != record["metadata"]
                        or [list(tag) for tag in self.target.tags(bucket, key)] != record["tags"]):
                    raise MigrationError("Target changed during verification")
        if listed_buckets(self.target, ignore_buckets=allowed) != expected_buckets:
            raise MigrationError("Target buckets changed during verification")
        return self._manifest_report(manifest)

    def _check_target_extras(self, snapshot):
        if self.allowed_extra_buckets & snapshot.keys():
            raise MigrationError("Cannot exclude source buckets")
        buckets = listed_buckets(self.target, ignore_buckets=self.allowed_extra_buckets)
        if buckets - snapshot.keys():
            raise MigrationError("Unexpected target buckets")
        for bucket in sorted(buckets):
            # Only the explicitly proven, managed FS copy target may contain
            # non-live upload artifacts from an interrupted copy or repair.
            guard_bucket(self.target, bucket, target_release=self.target_release,
                         inspect_uploads=not (
                             self._legacy_fs and self.target_release == "1.0.1"))
            objects = listed_objects(self.target, bucket)
            if objects.keys() - snapshot[bucket].keys():
                raise MigrationError("Unexpected target objects")
        return buckets

    def _replace_metadata(self, bucket, key, record):
        """Replace rclone's synthesized metadata without changing object bytes."""
        head = {name.lower(): value for name, value in
                self.target.request("HEAD", bucket, key)[1].items()}
        if not head.get("etag"):
            raise MigrationError("Missing target copy consistency header")
        copy_headers = {
            "x-amz-copy-source": "/" + urllib.parse.quote(bucket, safe="") + "/"
                                 + urllib.parse.quote(key, safe="/~"),
            "x-amz-copy-source-if-match": head["etag"],
        }
        # Pinned RustFS uses a metadata-only same-key path for the unencrypted,
        # unversioned, non-tiered objects admitted by our guards, at any size.
        if self.target_release == "1.0.1" or record["size"] <= 5 * 1024 ** 3:
            headers = dict(record["metadata"], **copy_headers,
                           **{"x-amz-metadata-directive": "REPLACE",
                              "x-amz-tagging-directive": "COPY"})
            xml(self.target.request("PUT", bucket, key, headers=headers)[2], "CopyObjectResult")
            return
        upload = xml(self.target.request(
            "POST", bucket, key, {"uploads": ""}, headers=record["metadata"])[2],
            "InitiateMultipartUploadResult")
        upload_id = upload.findtext("UploadId")
        if not upload_id:
            raise MigrationError("Missing metadata-repair upload ID")
        completed = False
        try:
            parts = ET.Element("CompleteMultipartUpload")
            chunk_size = 1024 ** 3
            for number, offset in enumerate(range(0, record["size"], chunk_size), 1):
                headers = dict(copy_headers, **{
                    "x-amz-copy-source-range": "bytes=%d-%d" % (
                        offset, min(offset + chunk_size, record["size"]) - 1)})
                result = xml(self.target.request(
                    "PUT", bucket, key, {"uploadId": upload_id, "partNumber": number},
                    headers=headers)[2], "CopyPartResult")
                etag = result.findtext("ETag")
                if not etag:
                    raise MigrationError("Missing metadata-repair part ETag")
                part = ET.SubElement(parts, "Part")
                ET.SubElement(part, "PartNumber").text = str(number)
                ET.SubElement(part, "ETag").text = etag
            xml(self.target.request(
                "POST", bucket, key, {"uploadId": upload_id}, ET.tostring(parts),
                {"content-type": "application/xml"})[2], "CompleteMultipartUploadResult")
            completed = True
        finally:
            if not completed:
                try:
                    self.target.request("DELETE", bucket, key, {"uploadId": upload_id})
                except S3Error as error:
                    if error.status != 404 or error.code != "NoSuchUpload":
                        raise MigrationError("Cannot abort metadata-repair upload") from None

    def run(self):
        snapshot = self._source_snapshot()
        existing = self._check_target_extras(snapshot)
        for bucket in sorted(snapshot):
            if bucket not in existing:
                self.target.request("PUT", bucket)
            self._rclone("copy", "src:" + bucket, "dst:" + bucket,
                         "--metadata", "--ignore-times", "--disable", "Copy",
                         "--transfers", "1", "--checkers", "8",
                         "--s3-upload-cutoff", "64Mi", "--s3-chunk-size", "16Mi",
                         "--s3-upload-concurrency", "2", "--retries", "3",
                         "--low-level-retries", "3", "--retries-sleep", "5s")
            for key, record in snapshot[bucket].items():
                # Clear stale tags as well as restoring non-empty tag sets.
                self.target.put_tags(bucket, key, record["tags"])
        if listed_buckets(self.target, ignore_buckets=self.allowed_extra_buckets) != snapshot.keys():
            raise MigrationError("Target bucket inventory differs")
        hashes = {}
        for bucket, records in snapshot.items():
            expected = {key: record["size"] for key, record in records.items()}
            if (listed_objects(self.target, bucket) != expected
                    or self._inventory("dst", bucket) != expected):
                raise MigrationError("Target object inventory differs")
            hashes[bucket] = {}
            for key, record in records.items():
                private_acl(self.target, bucket, key, target_release=self.target_release)
                metadata = object_metadata(self.target, bucket, key, record["size"])
                if metadata != record["metadata"]:
                    self._replace_metadata(bucket, key, record)
                    self.target.put_tags(bucket, key, record["tags"])
                    metadata = object_metadata(self.target, bucket, key, record["size"])
                if metadata != record["metadata"]:
                    raise MigrationError("Target object metadata differs")
                if self.target.tags(bucket, key) != record["tags"]:
                    raise MigrationError("Target object tags differ")
                source_hash = self.source.request("GET", bucket, key, stream=True)[2]
                target_hash = self.target.request("GET", bucket, key, stream=True)[2]
                if (source_hash != target_hash or source_hash["size"] != record["size"]):
                    raise MigrationError("Object content SHA-256 differs")
                hashes[bucket][key] = source_hash["sha256"]
        if self._source_snapshot() != snapshot:
            raise MigrationError("Source changed during migration")
        self._check_target_extras(snapshot)
        manifest = json.dumps({"inventory": snapshot, "sha256": hashes},
                              sort_keys=True, separators=(",", ":")).encode()
        return {"buckets": len(snapshot),
                "objects": sum(len(records) for records in snapshot.values()),
                "bytes": sum(record["size"] for records in snapshot.values()
                             for record in records.values()),
                "inventory_sha256": hashlib.sha256(manifest).hexdigest()}
