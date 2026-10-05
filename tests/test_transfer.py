"""Pure unit tests: no network, rclone binary, secrets, or filesystem fixtures."""

import copy
import hashlib
import io
import json
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from migration import transfer as m


LEGACY_ACL = (
    b'<AccessControlPolicy><Owner><ID></ID><DisplayName></DisplayName></Owner>'
    b'<AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    b' xsi:type="CanonicalUser"><Type>CanonicalUser</Type></Grantee>'
    b'<Permission>FULL_CONTROL</Permission></Grant></AccessControlList></AccessControlPolicy>')

RUSTFS_ACL = (
    b'<AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b'<Owner><DisplayName>rustfs</DisplayName>'
    b'<ID>c19050dbcee97fda828689dda99097a6321af2248fa760517237346e5d9c8a66</ID></Owner>'
    b'<AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    b' xsi:type="CanonicalUser"/><Permission>FULL_CONTROL</Permission></Grant>'
    b'</AccessControlList></AccessControlPolicy>')


def document(name, children=()):
    root = ET.Element(name)
    for key, value in children:
        ET.SubElement(root, key).text = str(value)
    return ET.tostring(root)


def listing(entries=(), truncated="false", token=None, encoding=None):
    root = ET.Element("ListBucketResult")
    for key, size in entries:
        node = ET.SubElement(root, "Contents")
        ET.SubElement(node, "Key").text = key
        ET.SubElement(node, "Size").text = str(size)
    if truncated is not None:
        ET.SubElement(root, "IsTruncated").text = truncated
    if token is not None:
        ET.SubElement(root, "NextContinuationToken").text = token
    if encoding is not None:
        ET.SubElement(root, "EncodingType").text = encoding
    return ET.tostring(root)


def obj(body=b"payload", metadata=None, tags=None):
    return {"body": body, "metadata": metadata or {"content-type": "application/octet-stream",
                                                 "x-amz-meta-custom": "value"},
            "tags": tags or []}


class MemoryS3:
    def __init__(self, port, buckets=None):
        self.endpoint = "http://127.0.0.1:" + str(port)
        self.key, self.secret, self.region = "access-sensitive", "secret-sensitive", "us-east-1"
        self.buckets = copy.deepcopy(buckets or {})
        self.calls = []
        self.config = {}
        self.bad_metadata = False
        self.bad_tags = False
        self.bad_hash = False
        self.acl = document("AccessControlPolicy")
        self.legacy_acl = False
        self.acl_body = None

    def request(self, method, bucket="", key="", query=None, body=b"", headers=None,
                stream=False, **kwargs):
        self.calls.append((method, bucket, key, query))
        query = query or {}
        if not bucket:
            root = ET.Element("ListAllMyBucketsResult")
            nodes = ET.SubElement(root, "Buckets")
            for name in self.buckets:
                ET.SubElement(ET.SubElement(nodes, "Bucket"), "Name").text = name
            return 200, {}, ET.tostring(root)
        if method == "PUT":
            if headers and headers.get("x-amz-metadata-directive") == "REPLACE":
                self.buckets[bucket][key]["metadata"] = {
                    name: value for name, value in headers.items()
                    if name.startswith("x-amz-meta-") or name in {
                        "content-type", "cache-control", "content-disposition", "content-encoding",
                        "content-language", "expires"}}
                return 200, {}, document("CopyObjectResult", [("ETag", "repaired")])
            self.buckets.setdefault(bucket, {})
            return 200, {}, b""
        if "acl" in query:
            if self.acl_body is not None:
                return 200, {}, self.acl_body
            if self.legacy_acl:
                return 200, {}, LEGACY_ACL
            return 200, {}, (
                b"<AccessControlPolicy><Owner><ID>owner</ID></Owner><AccessControlList>"
                b"<Grant><Grantee><ID>owner</ID></Grantee><Permission>FULL_CONTROL</Permission>"
                b"</Grant></AccessControlList></AccessControlPolicy>")
        if "versions" in query:
            if isinstance(self.config.get("versions"), Exception):
                raise self.config["versions"]
            return 200, {}, self.config.get("versions", document(
                "ListVersionsResult", [("IsTruncated", "false")]))
        if "uploads" in query:
            return 200, {}, self.config.get("uploads", document(
                "ListMultipartUploadsResult", [("IsTruncated", "false")]))
        for feature in (*m.BUCKET_ROOTS, "policy"):
            if feature in query:
                value = self.config.get(feature)
                if isinstance(value, Exception):
                    raise value
                return 200, {}, value if value is not None else (
                    b"{}" if feature == "policy" else document(m.BUCKET_ROOTS[feature]))
        if "list-type" in query:
            return 200, {}, listing([(k, len(v["body"]))
                                     for k, v in self.buckets[bucket].items()])
        value = self.buckets[bucket][key]
        if method == "HEAD":
            metadata = dict(value["metadata"], **{"content-length": str(len(value["body"]))})
            metadata["etag"] = hashlib.sha256(value["body"]).hexdigest()
            metadata["last-modified"] = "Sat, 03 Oct 2026 00:00:00 GMT"
            if self.bad_metadata:
                metadata["x-amz-meta-custom"] = "wrong"
            return 200, metadata, b""
        if stream:
            digest = hashlib.sha256(value["body"]).hexdigest()
            return 200, {}, {"size": len(value["body"]),
                            "sha256": "wrong" if self.bad_hash else digest}
        raise AssertionError("Unexpected request")

    def tags(self, bucket, key):
        return [("wrong", "tag")] if self.bad_tags else self.buckets[bucket][key]["tags"]

    def put_tags(self, bucket, key, tags):
        self.buckets[bucket][key]["tags"] = copy.deepcopy(tags)


class Harness(m.Transfer):
    def __init__(self, source, target, **kwargs):
        super().__init__(source, target, **kwargs)
        self.omit = set()
        self.rclone_calls = []
        self.fail_copy = False
        self.copy_extra_metadata = {}

    def _rclone(self, *args):
        self.rclone_calls.append(args)
        if args[0] == "lsjson":
            remote, bucket = args[1].split(":", 1)
            client = self.source if remote == "src" else self.target
            return json.dumps([{"Path": key, "Size": len(value["body"]), "IsDir": False}
                               for key, value in client.buckets[bucket].items()
                               if key not in self.omit]).encode()
        if args[0] == "copy":
            bucket = args[1].split(":", 1)[1]
            for key, value in self.source.buckets[bucket].items():
                self.target.buckets[bucket][key] = copy.deepcopy(value)
                self.target.buckets[bucket][key]["metadata"].update(self.copy_extra_metadata)
                if self.fail_copy:
                    raise m.MigrationError("Interrupted")
            return b""
        raise AssertionError(args)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.source = MemoryS3(9002, {"data": {"one": obj(tags=[("a", "b")]),
                                             "nested/two": obj(b"two")},
                                     "empty": {}})
        self.target = MemoryS3(8333)
        self.engine = Harness(self.source, self.target)

    def test_preflight_read_only_counts_live_bytes(self):
        self.assertEqual(self.engine.preflight(), 10)
        self.assertEqual(self.target.calls, [])
        self.assertTrue(all(call[0] in ("GET", "HEAD") for call in self.source.calls))
        self.assertTrue(all(call[0] == "lsjson" for call in self.engine.rclone_calls))

    def test_run_empty_buckets_exact_report_and_idempotency(self):
        report = self.engine.run()
        self.assertEqual({k: report[k] for k in ("buckets", "objects", "bytes")},
                         {"buckets": 2, "objects": 2, "bytes": 10})
        self.assertEqual(len(report["inventory_sha256"]), 64)
        self.assertEqual(self.engine.run(), report)
        self.assertEqual(self.target.buckets, self.source.buckets)
        self.assertIn("--ignore-times", next(c for c in self.engine.rclone_calls if c[0] == "copy"))
        for command in self.engine.rclone_calls:
            if command[0] == "copy":
                self.assertEqual(command[command.index("--transfers") + 1], "1")
                self.assertEqual(command[command.index("--s3-upload-concurrency") + 1], "2")
        self.assertFalse(any(call[0] == "DELETE" for call in self.target.calls))

    def test_no_source_buckets(self):
        self.source.buckets = {}
        self.assertEqual(self.engine.run()["bytes"], 0)
        self.assertEqual(self.target.buckets, {})

    def test_restart_after_partial_copy_repairs_stale_metadata_and_tags(self):
        self.engine.fail_copy = True
        with self.assertRaisesRegex(m.MigrationError, "Interrupted"):
            self.engine.run()
        self.target.buckets["data"]["one"]["metadata"] = {"content-type": "wrong"}
        self.target.buckets["data"]["one"]["tags"] = [("stale", "value")]
        self.engine.fail_copy = False
        self.engine.run()
        self.assertEqual(self.target.buckets, self.source.buckets)

    def test_stale_empty_tags_cleared(self):
        self.target.buckets = copy.deepcopy(self.source.buckets)
        self.target.buckets["data"]["nested/two"]["tags"] = [("stale", "value")]
        self.engine.run()
        self.assertEqual(self.target.tags("data", "nested/two"), [])

    def test_reject_extra_bucket_and_object_before_writes(self):
        for buckets in ({"extra": {}}, {"data": {"unexpected": obj()}}):
            with self.subTest(buckets=buckets):
                self.target.buckets = buckets
                self.target.calls.clear()
                with self.assertRaisesRegex(m.MigrationError, "Unexpected target"):
                    self.engine.run()
                self.assertFalse(any(call[0] == "PUT" for call in self.target.calls))

    def test_omitted_source_key_and_directory_marker_fail_preflight(self):
        for key in ("one", "folder/"):
            with self.subTest(key=key):
                self.source.buckets["data"][key] = obj(b"")
                self.engine.omit = {key}
                with self.assertRaisesRegex(m.MigrationError, "omitted"):
                    self.engine.preflight()
                self.assertEqual(self.target.calls, [])

    def test_metadata_tag_and_all_content_mismatches(self):
        for flag, message in (("bad_metadata", "metadata"), ("bad_tags", "tags"),
                              ("bad_hash", "SHA-256")):
            with self.subTest(flag=flag):
                setattr(self.target, flag, True)
                with self.assertRaisesRegex(m.MigrationError, message):
                    self.engine.run()
                setattr(self.target, flag, False)

    def test_missing_target_objects_are_not_accepted(self):
        original = self.engine._rclone

        def incomplete(*args):
            result = original(*args)
            if args[0] == "copy":
                self.target.buckets["data"].pop("nested/two", None)
            return result

        self.engine._rclone = incomplete
        # Simulate tag endpoints accepting writes independently of object visibility.
        self.target.put_tags = Mock()
        with self.assertRaisesRegex(m.MigrationError, "inventory"):
            self.engine.run()

    def test_explicit_mtime_metadata_preserved(self):
        self.source.buckets["data"]["one"]["metadata"]["x-amz-meta-mtime"] = "1234"
        self.engine.run()
        self.assertEqual(self.target.buckets["data"]["one"]["metadata"]["x-amz-meta-mtime"], "1234")

    def test_rclone_synthesized_times_are_replaced_not_ignored(self):
        self.engine.copy_extra_metadata = {
            "x-amz-meta-btime": "2026-10-04T00:05:20.27Z",
            "x-amz-meta-mtime": "1791072320.27"}
        report = self.engine.run()
        self.assertEqual(self.target.buckets, self.source.buckets)
        repairs = [call for call in self.target.calls if call[0] == "PUT" and call[2]]
        self.assertEqual(len(repairs), 2)
        self.assertEqual(self.engine.run(), report)

    def test_intentional_source_times_preserved_exactly_during_repair(self):
        self.source.buckets["data"]["one"]["metadata"].update({
            "x-amz-meta-btime": "intentionally-not-a-date", "x-amz-meta-mtime": "1234.56789"})
        self.engine.copy_extra_metadata = {
            "x-amz-meta-btime": "synthesized", "x-amz-meta-mtime": "99999"}
        self.engine.run()
        self.assertEqual(self.target.buckets, self.source.buckets)

    def test_incomplete_target_upload_does_not_block_non_destructive_retry(self):
        self.target.buckets = {"data": {"one": obj(b"corrupt")}}
        self.target.config["uploads"] = (
            b"<ListMultipartUploadsResult><Upload><Key>one</Key></Upload>"
            b"<IsTruncated>false</IsTruncated></ListMultipartUploadsResult>")
        self.engine = Harness(self.source, self.target, source_format="fs",
                              source_release=m.LEGACY_FS_RELEASE, target_release="1.0.1")
        self.engine.run()
        self.assertEqual(self.target.buckets, self.source.buckets)
        self.assertFalse(any(call[0] == "DELETE" for call in self.target.calls))

    def test_unproven_or_non_fs_target_pending_uploads_remain_fatal(self):
        self.target.buckets = {"data": {}}
        self.target.config["uploads"] = (
            b"<ListMultipartUploadsResult><Upload><Key>one</Key></Upload>"
            b"<IsTruncated>false</IsTruncated></ListMultipartUploadsResult>")
        proofs = ({}, {"source_format": "fs", "source_release": m.LEGACY_FS_RELEASE},
                  {"target_release": "1.0.1"},
                  {"source_format": "xl-single", "source_release": m.MODERN_XL_RELEASE,
                   "target_release": "1.0.1"})
        for proof in proofs:
            with self.subTest(proof=proof):
                with self.assertRaisesRegex(m.MigrationError, "pending multipart"):
                    Harness(self.source, self.target, **proof).run()
        self.assertFalse(any(call[0] in ("PUT", "DELETE") for call in self.target.calls))

    def test_proven_fs_source_pending_uploads_always_rejected(self):
        self.source.config["uploads"] = (
            b"<ListMultipartUploadsResult><Upload><Key>one</Key></Upload>"
            b"<IsTruncated>false</IsTruncated></ListMultipartUploadsResult>")
        self.engine = Harness(self.source, self.target, source_format="fs",
                              source_release=m.LEGACY_FS_RELEASE, target_release="1.0.1")
        for operation in (self.engine.preflight, self.engine.capture_manifest, self.engine.run):
            with self.assertRaisesRegex(m.MigrationError, "pending multipart"):
                operation()
        self.assertEqual(self.target.calls, [])

    def test_source_changes_during_copy_detected(self):
        original = self.engine._rclone

        def changed(*args):
            result = original(*args)
            if args[0] == "copy":
                self.source.buckets["data"]["one"]["tags"] = [("new", "tag")]
            return result

        self.engine._rclone = changed
        with self.assertRaisesRegex(m.MigrationError, "Source changed"):
            self.engine.run()

    def test_same_size_source_content_change_after_hash_detected(self):
        original = self.target.request

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("stream") and args[2] == "one":
                self.source.buckets["data"]["one"]["body"] = b"PAYLOAD"
            return result

        self.target.request = changed
        with self.assertRaisesRegex(m.MigrationError, "Source changed"):
            self.engine.run()

    def test_unsupported_bucket_features_fail_before_write(self):
        for feature, root in m.BUCKET_ROOTS.items():
            with self.subTest(feature=feature):
                self.source.config = {feature: document(root, [("Enabled", "true")])}
                with self.assertRaisesRegex(m.MigrationError, "Unsupported bucket"):
                    self.engine.run()
                self.assertEqual(self.target.calls, [])

    def test_policy_history_and_denied_inspection_rejected(self):
        values = [
            {"policy": b'{"Statement":[]}'},
            {"versions": b"<ListVersionsResult><DeleteMarker/></ListVersionsResult>"},
            {"versions": b"<ListVersionsResult><Version><VersionId>1</VersionId>"
                         b"</Version></ListVersionsResult>"},
            {"uploads": b"<ListMultipartUploadsResult><Upload/></ListMultipartUploadsResult>"},
            {"encryption": m.S3Error(403, "secret-sensitive")},
            {"encryption": m.S3Error(501, "NotImplemented")},
        ]
        for config in values:
            with self.subTest(config=config):
                self.source.config = config
                with self.assertRaises(m.MigrationError):
                    self.engine.preflight()
        self.assertEqual(self.target.calls, [])

    def test_known_absence_only_is_accepted(self):
        self.source.config = {"encryption": m.S3Error(
            404, "ServerSideEncryptionConfigurationNotFoundError")}
        self.assertEqual(self.engine.preflight(), 10)
        self.source.config = {"encryption": m.S3Error(404, "Unknown")}
        with self.assertRaises(m.MigrationError):
            self.engine.preflight()

    def test_object_encryption_retention_versions_and_tiers_rejected(self):
        for name, value in (("x-amz-server-side-encryption", "AES256"),
                            ("x-amz-object-lock-retain-until-date", "tomorrow"),
                            ("x-amz-object-lock-legal-hold", "ON"),
                            ("x-amz-version-id", "1"), ("x-amz-storage-class", "GLACIER"),
                            ("x-amz-website-redirect-location", "/other")):
            with self.subTest(name=name):
                metadata = self.source.buckets["data"]["one"]["metadata"]
                metadata[name] = value
                with self.assertRaisesRegex(m.MigrationError, "Unsupported object"):
                    self.engine.preflight()
                del metadata[name]


class ListingTests(unittest.TestCase):
    def client(self, *bodies):
        return Mock(request=Mock(side_effect=[(200, {}, body) for body in bodies]))

    def test_pagination_url_keys_and_continuation(self):
        client = self.client(listing([("a%2Fb%25%00", 0)], "true", "opaque", "url"),
                             listing([("z", 12)]))
        self.assertEqual(m.listed_objects(client, "data"), {"a/b%\x00": 0, "z": 12})
        self.assertEqual(client.request.call_args.kwargs["query"]["continuation-token"], "opaque")

    def test_incomplete_duplicate_and_cyclic_pages(self):
        cases = [
            [listing(truncated=None)], [listing(truncated="maybe")],
            [listing(truncated="true")], [listing([("a", -1)])],
            [listing([("a", 1), ("a", 1)])],
            [listing([], "true", "x"), listing([], "true", "x")],
            [listing([], "true", "x"), listing([], "true", "y"), listing([], "true", "x")],
            [b"<ListBucketResult><IsTruncated>false</IsTruncated>"
             b"<CommonPrefixes><Prefix>x</Prefix></CommonPrefixes></ListBucketResult>"],
            [listing([("%bad%", 1)], encoding="url")],
        ]
        for bodies in cases:
            with self.subTest(bodies=bodies):
                with self.assertRaises(m.MigrationError):
                    m.listed_objects(self.client(*bodies), "data")

    def test_wrong_xml_and_incomplete_buckets(self):
        for body in (b"invalid", b"<Other/>", b"<ListAllMyBucketsResult/>"):
            with self.assertRaises(m.MigrationError):
                m.listed_buckets(self.client(body))

    def test_nonprivate_acl_rejected(self):
        client = self.client(
            b"<AccessControlPolicy><Owner><ID>a</ID></Owner><AccessControlList><Grant>"
            b"<Grantee><URI>AllUsers</URI></Grantee><Permission>READ</Permission></Grant>"
            b"</AccessControlList></AccessControlPolicy>")
        with self.assertRaisesRegex(m.MigrationError, "ACL"):
            m.private_acl(client, "data")

    def test_version_pagination_cycle_rejected(self):
        client = MemoryS3(9002, {"data": {}})
        client.config["versions"] = document("ListVersionsResult",
                                            [("IsTruncated", "true"), ("NextKeyMarker", "a")])
        with self.assertRaisesRegex(m.MigrationError, "pagination"):
            m.guard_bucket(client, "data")

    def test_object_listing_and_head_sizes_must_match(self):
        client = self.client(b"")
        client.request.return_value = (200, {"content-length": "3"}, b"")
        client.request.side_effect = None
        with self.assertRaisesRegex(m.MigrationError, "sizes differ"):
            m.object_metadata(client, "data", "one", 4)

    def test_default_request_payment_is_supported(self):
        client = MemoryS3(9002, {"data": {}})
        client.config["requestPayment"] = document(
            "RequestPaymentConfiguration", [("Payer", "BucketOwner")])
        m.guard_bucket(client, "data")

    def test_tags_round_trip_and_empty_tag_clear(self):
        client = m.S3("http://127.0.0.1:9002", "key", "secret")
        with patch.object(client, "request", return_value=(200, {}, b"")) as request:
            client.put_tags("data", "one", [("a<&", "b")])
            body = request.call_args.args[4]
            self.assertEqual(m.xml(body).findtext("./TagSet/Tag/Key"), "a<&")
            self.assertIn("content-md5", request.call_args.args[5])
            client.put_tags("data", "one", [])
            self.assertEqual(m.xml(request.call_args.args[4]).findall("./TagSet/Tag"), [])
        with patch.object(client, "request", return_value=(200, {}, body)):
            self.assertEqual(client.tags("data", "one"), [("a<&", "b")])
        with patch.object(client, "request", return_value=(200, {}, b"<Tagging/>")):
            with self.assertRaisesRegex(m.MigrationError, "Incomplete"):
                client.tags("data", "one")

    def test_invalid_rclone_inventories(self):
        engine = m.Transfer(MemoryS3(9002), MemoryS3(8333))
        for value in ({}, [None], [{"Path": "a", "Size": True, "IsDir": False}],
                      [{"Path": "a", "Size": 0}],
                      [{"Path": "a", "Size": -1, "IsDir": False}]):
            with self.subTest(value=value):
                with patch.object(engine, "_rclone", return_value=json.dumps(value).encode()):
                    with self.assertRaises(m.MigrationError):
                        engine._inventory("src", "data")


class LegacyReaderTests(unittest.TestCase):
    def setUp(self):
        self.source = MemoryS3(9002, {"data": {"one": obj()}, "empty": {}})
        self.target = MemoryS3(8333)
        self.source.legacy_acl = True
        self.source.config = {
            "ownershipControls": m.S3Error(501, "NotImplemented"),
            "versions": m.S3Error(501, "NotImplemented"),
        }

    def engine(self, **kwargs):
        return Harness(self.source, self.target, **kwargs)

    def test_exact_pinned_fs_fixture_preflight_and_run(self):
        engine = self.engine(source_format="fs", source_release=m.LEGACY_FS_RELEASE)
        self.assertEqual(engine.preflight(), 7)
        self.assertEqual(self.target.calls, [])
        report = engine.run()
        self.assertEqual(report["objects"], 1)
        self.assertEqual(report["buckets"], 2)
        self.assertEqual(self.target.buckets, self.source.buckets)

    def test_no_proof_partial_proof_other_format_or_release_are_strict(self):
        proofs = ({}, {"source_format": "fs"}, {"source_release": m.LEGACY_FS_RELEASE},
                  {"source_format": "xl", "source_release": m.LEGACY_FS_RELEASE},
                  {"source_format": "fs", "source_release": "RELEASE.2022-05-26T05-48-41Z"})
        for proof in proofs:
            with self.subTest(proof=proof):
                with self.assertRaises(m.MigrationError):
                    self.engine(**proof).preflight()
        self.assertEqual(self.target.calls, [])

    def test_each_exception_remains_strict_independently(self):
        cases = ("ownershipControls", "versions", "acl")
        for feature in cases:
            with self.subTest(feature=feature):
                self.source.config = {}
                self.source.legacy_acl = False
                if feature == "acl":
                    self.source.legacy_acl = True
                else:
                    self.source.config[feature] = m.S3Error(501, "NotImplemented")
                with self.assertRaises(m.MigrationError):
                    self.engine().preflight()

    def test_only_501_notimplemented_is_exempt(self):
        for feature in ("ownershipControls", "versions"):
            for status, code in ((403, "AccessDenied"), (404, "NotImplemented"),
                                 (500, "NotImplemented"), (501, "Other")):
                with self.subTest(feature=feature, status=status, code=code):
                    self.source.config = {feature: m.S3Error(status, code)}
                    with self.assertRaises(m.MigrationError):
                        self.engine(source_format="fs",
                                    source_release=m.LEGACY_FS_RELEASE).preflight()

    def test_no_broad_501_exception_for_other_features(self):
        for feature in (*m.BUCKET_ROOTS, "policy"):
            if feature == "ownershipControls":
                continue
            with self.subTest(feature=feature):
                self.source.config = {feature: m.S3Error(501, "NotImplemented")}
                with self.assertRaises(m.MigrationError):
                    self.engine(source_format="fs",
                                source_release=m.LEGACY_FS_RELEASE).preflight()

    def test_empty_status_allowed_but_enabled_suspended_or_mfa_metadata_fail(self):
        engine = self.engine(source_format="fs", source_release=m.LEGACY_FS_RELEASE)
        self.source.config["versioning"] = document("VersioningConfiguration", [("Status", "")])
        self.assertEqual(engine.preflight(), 7)
        for field, value in (("Status", "Enabled"), ("Status", "Suspended"),
                             ("MfaDelete", "Disabled")):
            with self.subTest(field=field, value=value):
                self.source.config["versioning"] = document(
                    "VersioningConfiguration", [(field, value)])
                with self.assertRaises(m.MigrationError):
                    engine.preflight()

    def test_object_version_and_encryption_evidence_not_exempt(self):
        engine = self.engine(source_format="fs", source_release=m.LEGACY_FS_RELEASE)
        for name, value in (("x-amz-version-id", "old-version"),
                            ("x-amz-server-side-encryption", "AES256"),
                            ("x-amz-object-lock-mode", "COMPLIANCE")):
            with self.subTest(name=name):
                self.source.buckets["data"]["one"]["metadata"][name] = value
                with self.assertRaises(m.MigrationError):
                    engine.preflight()
                del self.source.buckets["data"]["one"]["metadata"][name]

    def test_synthetic_acl_requires_exact_shape(self):
        mutations = [
            LEGACY_ACL.replace(b"FULL_CONTROL", b"READ"),
            LEGACY_ACL.replace(b"<ID></ID>", b"<ID>unknown</ID>"),
            LEGACY_ACL.replace(b"<Type>CanonicalUser</Type>", b"<Type>Group</Type>"),
            LEGACY_ACL.replace(b'xsi:type="CanonicalUser"', b'xsi:type="Group"'),
            LEGACY_ACL.replace(b"</Grantee>", b"<URI>AllUsers</URI></Grantee>"),
            LEGACY_ACL.replace(b"</Grantee>", b"<ID></ID></Grantee>"),
            LEGACY_ACL.replace(b"</Owner>", b"<Other/></Owner>"),
            LEGACY_ACL.replace(b"</AccessControlList>", b"<Grant/></AccessControlList>"),
        ]
        for body in mutations:
            with self.subTest(body=body):
                client = Mock(request=Mock(return_value=(200, {}, body)))
                with self.assertRaisesRegex(m.MigrationError, "ACL"):
                    m.private_acl(client, "data", trusted_reader=True)
        client = Mock(request=Mock(return_value=(200, {}, LEGACY_ACL)))
        m.private_acl(client, "data", trusted_reader=True)
        with self.assertRaises(m.MigrationError):
            m.private_acl(client, "data")


class ModernReaderTests(unittest.TestCase):
    def setUp(self):
        self.source = MemoryS3(9002, {"data": {"one": obj()}})
        self.target = MemoryS3(8333, self.source.buckets)
        self.source.legacy_acl = True
        self.source.config["ownershipControls"] = m.S3Error(501, "NotImplemented")

    def engine(self, **kwargs):
        return m.Transfer(self.source, self.target, **kwargs)

    def test_exact_modern_xl_reader_capture_and_sequential_verify(self):
        for source_format in ("xl", "xl-single"):
            with self.subTest(source_format=source_format):
                engine = self.engine(source_format=source_format, source_release=m.MODERN_XL_RELEASE)
                with patch.object(engine, "_rclone", side_effect=AssertionError("No rclone")):
                    manifest = engine.capture_manifest()
                    self.assertEqual(engine.verify_manifest(manifest)["bytes"], 7)

    def test_modern_reader_without_matching_proof_is_strict(self):
        for proof in ({}, {"source_format": "xl"},
                      {"source_format": "xl", "source_release": m.LEGACY_FS_RELEASE},
                      {"source_format": "fs", "source_release": m.MODERN_XL_RELEASE},
                      {"source_release": m.MODERN_XL_RELEASE}):
            with self.subTest(proof=proof):
                with self.assertRaises(m.MigrationError):
                    self.engine(**proof).capture_manifest()

    def test_modern_xl_never_exempts_version_history_listing(self):
        engine = self.engine(source_format="xl", source_release=m.MODERN_XL_RELEASE)
        for response in (
                m.S3Error(501, "NotImplemented"), m.S3Error(404, "NotImplemented"),
                b"<ListVersionsResult><Version><VersionId>old</VersionId></Version>"
                b"</ListVersionsResult>",
                b"<ListVersionsResult><DeleteMarker/></ListVersionsResult>"):
            with self.subTest(response=response):
                self.source.config["versions"] = response
                with self.assertRaises(m.MigrationError):
                    engine.capture_manifest()

    def test_modern_reader_other_ownership_errors_not_exempt(self):
        engine = self.engine(source_format="xl", source_release=m.MODERN_XL_RELEASE)
        for status, code in ((403, "NotImplemented"), (500, "NotImplemented"), (501, "Other")):
            with self.subTest(status=status, code=code):
                self.source.config["ownershipControls"] = m.S3Error(status, code)
                with self.assertRaises(m.MigrationError):
                    engine.capture_manifest()

    def test_source_reader_proof_does_not_relax_destination_inspection(self):
        engine = self.engine(source_format="xl", source_release=m.MODERN_XL_RELEASE)
        manifest = engine.capture_manifest()
        self.target.legacy_acl = True
        with self.assertRaisesRegex(m.MigrationError, "ACL"):
            engine.verify_manifest(manifest)


class HistoricalWireResponseTests(unittest.TestCase):
    def test_genuine_acl_and_encryption_error_xml_through_s3_transport(self):
        encryption_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Error><Code>ServerSideEncryptionConfigurationNotFoundError</Code>'
            b'<Message>The server side encryption configuration was not found</Message>'
            b'<BucketName>data</BucketName></Error>')
        for source_format, release in (("fs", m.LEGACY_FS_RELEASE),
                                       ("xl-single", m.MODERN_XL_RELEASE)):
            with self.subTest(source_format=source_format, release=release):
                backend = MemoryS3(9002, {"data": {"one": obj(tags=[("a", "b")])}})
                backend.config["versions"] = (
                    b"<ListVersionsResult><IsTruncated>false</IsTruncated>"
                    b"<Version><Key>one</Key><VersionId>null</VersionId>"
                    b"<IsLatest>true</IsLatest></Version></ListVersionsResult>")
                source = m.S3(backend.endpoint, backend.key, backend.secret)
                target = m.S3("http://127.0.0.1:8333", "target-key", "target-secret")
                target._opener = Mock()

                def open_response(request, **kwargs):
                    parsed = m.urllib.parse.urlsplit(request.full_url)
                    query = m.urllib.parse.parse_qs(
                        parsed.query, keep_blank_values=True)
                    query = {name: values[0] for name, values in query.items()}
                    path = parsed.path.lstrip("/").split("/", 1)
                    bucket = m.urllib.parse.unquote(path[0])
                    key = m.urllib.parse.unquote(path[1]) if len(path) > 1 else ""
                    if "encryption" in query:
                        raise urllib.error.HTTPError(
                            request.full_url, 404, "Not Found", {},
                            io.BytesIO(encryption_xml))
                    if "ownershipControls" in query or (
                            source_format == "fs" and "versions" in query):
                        raise urllib.error.HTTPError(
                            request.full_url, 501, "Not Implemented", {},
                            io.BytesIO(b"<Error><Code>NotImplemented</Code></Error>"))
                    if "acl" in query:
                        status, headers, body = 200, {}, LEGACY_ACL
                    elif key and "tagging" in query:
                        root = ET.Element("Tagging")
                        tagset = ET.SubElement(root, "TagSet")
                        for name, value in backend.tags(bucket, key):
                            node = ET.SubElement(tagset, "Tag")
                            ET.SubElement(node, "Key").text = name
                            ET.SubElement(node, "Value").text = value
                        status, headers, body = 200, {}, ET.tostring(root)
                    elif key and request.get_method() == "GET" and not query:
                        status, headers, body = 200, {}, backend.buckets[bucket][key]["body"]
                    else:
                        status, headers, body = backend.request(
                            request.get_method(), bucket, key, query=query)
                    response = Mock(status=status, headers=headers)
                    response.read.side_effect = io.BytesIO(body).read
                    response.__enter__ = Mock(return_value=response)
                    response.__exit__ = Mock(return_value=False)
                    return response

                source._opener = Mock(open=Mock(side_effect=open_response))
                engine = m.Transfer(source, target, source_format=source_format,
                                    source_release=release)
                with patch.object(m.time, "sleep"):
                    manifest = engine.capture_manifest()
                    self.assertEqual(manifest["buckets"]["data"]["one"]["size"], 7)
                    self.assertEqual(manifest["buckets"]["data"]["one"]["tags"], [["a", "b"]])
                    with self.assertRaises(m.MigrationError):
                        m.Transfer(source, target).capture_manifest()
                target._opener.open.assert_not_called()


class DestinationProfileTests(unittest.TestCase):
    def wire_client(self, port, *, feature="encryption", status=400,
                    code="ServerSideEncryptionConfigurationNotFoundError", acl_body=None):
        backend = MemoryS3(port, {"data": {}})
        client = m.S3(backend.endpoint, backend.key, backend.secret)

        def open_response(request, **kwargs):
            parsed = m.urllib.parse.urlsplit(request.full_url)
            query = m.urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            query = {name: values[0] for name, values in query.items()}
            if feature in query:
                body = document("Error", [("Code", code)])
                raise urllib.error.HTTPError(request.full_url, status, "Request failed", {},
                                             io.BytesIO(body))
            if "acl" in query and acl_body is not None:
                response_status, headers, body = 200, {}, acl_body
            else:
                bucket = m.urllib.parse.unquote(parsed.path.lstrip("/"))
                response_status, headers, body = backend.request(
                    request.get_method(), bucket, query=query)
            response = Mock(status=response_status, headers=headers)
            response.read.side_effect = io.BytesIO(body).read
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return response

        client._opener = Mock(open=Mock(side_effect=open_response))
        return client

    def test_exact_rustfs_target_encryption_absence_through_transport(self):
        source = MemoryS3(9002)
        engine = m.Transfer(source, self.wire_client(8333), target_release="1.0.1")
        report = engine.verify_manifest({"schema": 1, "buckets": {"data": {}}})
        self.assertEqual(report["buckets"], 1)
        self.assertEqual(report["objects"], 0)
        self.assertEqual(source.calls, [])

    def test_other_target_profiles_statuses_codes_and_features_are_strict(self):
        absent = "ServerSideEncryptionConfigurationNotFoundError"
        cases = (
            (None, "encryption", 400, absent),
            ("1.0.0", "encryption", 400, absent),
            ("1.0.1-other", "encryption", 400, absent),
            ("1.0.1", "encryption", 403, absent),
            ("1.0.1", "encryption", 400, "InvalidRequest"),
            ("1.0.1", "encryption", 400, "NoSuchServerSideEncryptionConfiguration"),
            ("1.0.1", "cors", 400, "NoSuchCORSConfiguration"),
            ("1.0.1", "object-lock", 400, "ObjectLockConfigurationNotFoundError"),
        )
        for release, feature, status, code in cases:
            with self.subTest(release=release, feature=feature, status=status, code=code):
                target = self.wire_client(8333, feature=feature, status=status, code=code)
                engine = m.Transfer(MemoryS3(9002), target, target_release=release)
                with self.assertRaises(m.MigrationError):
                    engine.verify_manifest({"schema": 1, "buckets": {"data": {}}})

    def test_target_proof_does_not_relax_source_http400(self):
        source = self.wire_client(9002)
        engine = m.Transfer(source, MemoryS3(8333), target_release="1.0.1")
        with self.assertRaisesRegex(m.MigrationError, "Cannot inspect bucket encryption"):
            engine.capture_manifest()

    def test_exact_target_ownership_controls_notimplemented_through_transport(self):
        target = self.wire_client(8333, feature="ownershipControls", status=501,
                                  code="NotImplemented", acl_body=RUSTFS_ACL)
        engine = m.Transfer(MemoryS3(9002), target, target_release="1.0.1")
        with patch.object(m.time, "sleep"):
            self.assertEqual(engine.verify_manifest(
                {"schema": 1, "buckets": {"data": {}}})["buckets"], 1)

    def test_ownership_controls_exception_has_exact_target_boundaries(self):
        cases = (
            (None, "ownershipControls", 501, "NotImplemented"),
            ("1.0.0", "ownershipControls", 501, "NotImplemented"),
            ("1.0.1", "ownershipControls", 500, "NotImplemented"),
            ("1.0.1", "ownershipControls", 400, "NotImplemented"),
            ("1.0.1", "ownershipControls", 501, "Other"),
            ("1.0.1", "versioning", 501, "NotImplemented"),
            ("1.0.1", "encryption", 501, "NotImplemented"),
            ("1.0.1", "policy", 501, "NotImplemented"),
        )
        with patch.object(m.time, "sleep"):
            for release, feature, status, code in cases:
                with self.subTest(release=release, feature=feature, status=status, code=code):
                    target = self.wire_client(8333, feature=feature, status=status, code=code)
                    engine = m.Transfer(MemoryS3(9002), target, target_release=release)
                    with self.assertRaises(m.MigrationError):
                        engine.verify_manifest({"schema": 1, "buckets": {"data": {}}})

    def test_target_profile_does_not_relax_unproven_source_ownership_controls(self):
        source = self.wire_client(9002, feature="ownershipControls", status=501,
                                  code="NotImplemented")
        engine = m.Transfer(source, MemoryS3(8333), target_release="1.0.1")
        with patch.object(m.time, "sleep"):
            with self.assertRaisesRegex(m.MigrationError, "Cannot inspect bucket ownershipControls"):
                engine.capture_manifest()

    def test_destination_adaptation_applies_to_copy_retry_checks(self):
        source = MemoryS3(9002, {"data": {}})
        target = MemoryS3(8333, {"data": {}})
        target.config["encryption"] = m.S3Error(
            400, "ServerSideEncryptionConfigurationNotFoundError")
        engine = Harness(source, target, target_release="1.0.1")
        self.assertEqual(engine.run()["buckets"], 1)
        with self.assertRaises(m.MigrationError):
            Harness(source, target).run()

    def test_exact_target_acl_both_bucket_and_object_through_transport(self):
        target = self.wire_client(8333, acl_body=RUSTFS_ACL)
        for key in ("", "one"):
            m.private_acl(target, "data", key, target_release="1.0.1")
            for release in (None, "1.0.0", "1.0.1-other"):
                with self.subTest(key=key, release=release):
                    with self.assertRaisesRegex(m.MigrationError, "ACL"):
                        m.private_acl(target, "data", key, target_release=release)
        engine = m.Transfer(MemoryS3(9002), target, target_release="1.0.1")
        self.assertEqual(engine.verify_manifest(
            {"schema": 1, "buckets": {"data": {}}})["buckets"], 1)

    def test_target_acl_shape_never_accepted_as_source_acl(self):
        source = MemoryS3(9002, {"data": {}})
        source.acl_body = RUSTFS_ACL
        for source_format, release in (("fs", m.LEGACY_FS_RELEASE),
                                       ("xl", m.MODERN_XL_RELEASE)):
            with self.subTest(source_format=source_format):
                engine = m.Transfer(source, MemoryS3(8333), source_format=source_format,
                                    source_release=release, target_release="1.0.1")
                with self.assertRaisesRegex(m.MigrationError, "ACL"):
                    engine.capture_manifest()

    def test_target_acl_requires_fixed_owner_and_exact_private_shape(self):
        mutations = [
            RUSTFS_ACL.replace(b"FULL_CONTROL", b"READ"),
            RUSTFS_ACL.replace(b"c19050db", b"ffffffff"),
            RUSTFS_ACL.replace(b">rustfs<", b">other<"),
            RUSTFS_ACL.replace(b'xsi:type="CanonicalUser"', b'xsi:type="Group"'),
            RUSTFS_ACL.replace(b'CanonicalUser"/>', b'CanonicalUser"><ID>x</ID></Grantee>'),
            RUSTFS_ACL.replace(b'CanonicalUser"/>',
                               b'CanonicalUser"><URI>AllUsers</URI></Grantee>'),
            RUSTFS_ACL.replace(b'CanonicalUser"/>',
                               b'CanonicalUser"><Type>CanonicalUser</Type></Grantee>'),
            RUSTFS_ACL.replace(b"</AccessControlList>", b"<Grant/></AccessControlList>"),
            RUSTFS_ACL.replace(b"</Owner>", b"<Other/></Owner>"),
            RUSTFS_ACL.replace(b"2006-03-01/", b"wrong-namespace/"),
            RUSTFS_ACL.replace(b'CanonicalUser"/>', b'CanonicalUser" other="x"/>'),
        ]
        for body in mutations:
            with self.subTest(body=body):
                target = self.wire_client(8333, acl_body=body)
                with self.assertRaisesRegex(m.MigrationError, "ACL"):
                    m.private_acl(target, "data", target_release="1.0.1")

    def test_copy_and_manifest_check_target_object_acl_under_proof(self):
        source = MemoryS3(9002, {"data": {"one": obj()}})
        target = MemoryS3(8333)
        target.acl_body = RUSTFS_ACL
        target.config["encryption"] = m.S3Error(
            400, "ServerSideEncryptionConfigurationNotFoundError")
        engine = Harness(source, target, target_release="1.0.1")
        self.assertEqual(engine.run()["objects"], 1)
        self.assertEqual(engine.verify_manifest(engine.capture_manifest())["objects"], 1)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.source = MemoryS3(9002, {"data": {"one": obj(tags=[("a", "b")]),
                                             "directory/": obj(b"")},
                                     "empty": {}})
        self.target = MemoryS3(8333, self.source.buckets)
        self.engine = m.Transfer(self.source, self.target)

    def capture(self):
        with patch.object(self.engine, "_rclone", side_effect=AssertionError("No rclone")):
            return self.engine.capture_manifest()

    def test_capture_source_only_no_rclone_writes_or_credentials(self):
        manifest = self.capture()
        self.assertEqual(self.target.calls, [])
        self.assertTrue(all(call[0] in ("GET", "HEAD") for call in self.source.calls))
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["buckets"]["data"]["one"]["tags"], [["a", "b"]])
        self.assertEqual(manifest["buckets"]["data"]["directory/"]["size"], 0)
        encoded = json.dumps(manifest)
        self.assertNotIn("secret-sensitive", encoded)
        self.assertNotIn("access-sensitive", encoded)
        self.assertNotIn("127.0.0.1", encoded)
        self.assertEqual(json.loads(encoded), manifest)

    def test_verify_source_stopped_target_read_only_and_internal_bucket_excluded(self):
        manifest = self.capture()
        control = "open-balena-storage-" + "a" * 32
        self.target.buckets[control] = {"identity.json": obj(b"parent-owned")}
        with patch.object(self.source, "request", side_effect=AssertionError("Source stopped")):
            with patch.object(self.engine, "_rclone", side_effect=AssertionError("No rclone")):
                report = self.engine.verify_manifest(json.loads(json.dumps(manifest)),
                                                     allowed_extra_buckets=[control])
                self.assertEqual(self.engine.verify_manifest(manifest,
                                                             allowed_extra_buckets=[control]), report)
        self.assertEqual({k: report[k] for k in ("buckets", "objects", "bytes")},
                         {"buckets": 2, "objects": 2, "bytes": 7})
        self.assertEqual(len(report["inventory_sha256"]), 64)
        self.assertTrue(all(call[0] in ("GET", "HEAD") for call in self.target.calls))
        self.assertFalse(any(call[1] == control for call in self.target.calls))

    def test_control_bucket_requires_explicit_parent_permission(self):
        manifest = self.capture()
        control = "open-balena-storage-" + "a" * 32
        self.target.buckets[control] = {"identity.json": obj()}
        with self.assertRaisesRegex(m.MigrationError, "bucket inventory"):
            self.engine.verify_manifest(manifest)
        self.assertEqual(self.engine.verify_manifest(
            manifest, allowed_extra_buckets=[control])["objects"], 2)
        self.target.buckets["other"] = {}
        with self.assertRaisesRegex(m.MigrationError, "bucket inventory"):
            self.engine.verify_manifest(manifest, allowed_extra_buckets=[control])

    def test_no_implicit_backend_internal_bucket_exception(self):
        manifest = self.capture()
        self.target.buckets[".rustfs.sys"] = {}
        with self.assertRaises(m.MigrationError):
            self.engine.verify_manifest(manifest)

    def test_cannot_allow_source_bucket_or_malformed_allowlist(self):
        manifest = self.capture()
        for allowed in (["data"], "data", ["*"], [None], {"data": True}):
            with self.subTest(allowed=allowed):
                with self.assertRaises(m.MigrationError):
                    self.engine.verify_manifest(manifest, allowed_extra_buckets=allowed)

    def test_verify_extra_missing_buckets_and_keys_rejected(self):
        manifest = self.capture()
        initial = copy.deepcopy(self.target.buckets)
        cases = [
            lambda: self.target.buckets.update({"extra": {}}),
            lambda: self.target.buckets.pop("empty"),
            lambda: self.target.buckets["data"].update({"extra": obj()}),
            lambda: self.target.buckets["data"].pop("one"),
            lambda: self.target.buckets.update({".unknown.sys": {}}),
        ]
        for mutate in cases:
            self.target.buckets = copy.deepcopy(initial)
            mutate()
            with self.assertRaises(m.MigrationError):
                self.engine.verify_manifest(manifest)

    def test_verify_metadata_tags_same_size_hash_and_size_mismatches(self):
        manifest = self.capture()
        for flag, message in (("bad_metadata", "metadata"), ("bad_tags", "tags"),
                              ("bad_hash", "SHA-256")):
            with self.subTest(flag=flag):
                setattr(self.target, flag, True)
                with self.assertRaisesRegex(m.MigrationError, message):
                    self.engine.verify_manifest(manifest)
                setattr(self.target, flag, False)
        self.target.buckets["data"]["one"]["body"] = b"PAYLOAD"
        with self.assertRaisesRegex(m.MigrationError, "SHA-256"):
            self.engine.verify_manifest(manifest)
        self.target.buckets["data"]["one"]["body"] = b"larger-payload"
        with self.assertRaisesRegex(m.MigrationError, "inventory"):
            self.engine.verify_manifest(manifest)

    def test_manifest_verification_does_not_ignore_or_repair_added_times(self):
        manifest = self.capture()
        for name in ("x-amz-meta-mtime", "x-amz-meta-btime"):
            with self.subTest(name=name):
                self.target.buckets["data"]["one"]["metadata"][name] = "unexpected"
                with self.assertRaisesRegex(m.MigrationError, "metadata"):
                    self.engine.verify_manifest(manifest)
                del self.target.buckets["data"]["one"]["metadata"][name]
        self.assertFalse(any(call[0] not in ("GET", "HEAD") for call in self.target.calls))

    def test_native_verify_rejects_pending_uploads_even_with_destination_proof(self):
        manifest = self.capture()
        self.target.config["uploads"] = (
            b"<ListMultipartUploadsResult><Upload><Key>one</Key></Upload>"
            b"<IsTruncated>false</IsTruncated></ListMultipartUploadsResult>")
        for source_format, release in (("fs", m.LEGACY_FS_RELEASE),
                                       ("xl-single", m.MODERN_XL_RELEASE)):
            with self.subTest(source_format=source_format):
                engine = m.Transfer(self.source, self.target, source_format=source_format,
                                    source_release=release, target_release="1.0.1")
                with self.assertRaisesRegex(m.MigrationError, "pending multipart"):
                    engine.verify_manifest(manifest)
        self.assertFalse(any(call[0] not in ("GET", "HEAD") for call in self.target.calls))

    def test_invalid_manifests_fail_before_contacting_target(self):
        manifest = self.capture()
        bad_manifests = [None, {}, {"schema": True, "buckets": {}},
                         {"schema": 2, "buckets": {}},
                         {"schema": 1, "buckets": {}, "secret": "x"}]
        for field, value in (("size", True), ("size", -1), ("sha256", "bad"),
                             ("metadata", {"x": 1}), ("tags", [["x", "y"], ["x", "z"]]),
                             ("tags", [["x"]]), ("tags", [["z", "y"], ["a", "b"]])):
            bad = copy.deepcopy(manifest)
            bad["buckets"]["data"]["one"][field] = value
            bad_manifests.append(bad)
        for bad in bad_manifests:
            with self.subTest(manifest=bad):
                with self.assertRaises(m.MigrationError):
                    self.engine.verify_manifest(bad)
                self.assertEqual(self.target.calls, [])

    def test_source_change_during_capture_detected(self):
        original = self.source.request

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("stream"):
                self.source.buckets["data"]["one"]["body"] = b"PAYLOAD"
            return result

        self.source.request = changed
        with self.assertRaisesRegex(m.MigrationError, "Source changed"):
            self.capture()

    def test_target_change_after_hash_detected(self):
        manifest = self.capture()
        original = self.target.request

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("stream"):
                self.target.buckets["data"]["one"]["body"] = b"PAYLOAD"
            return result

        self.target.request = changed
        with self.assertRaisesRegex(m.MigrationError, "Target changed"):
            self.engine.verify_manifest(manifest)

    def test_capture_with_proven_fs_reader(self):
        self.source.legacy_acl = True
        self.source.config = {"ownershipControls": m.S3Error(501, "NotImplemented"),
                              "versions": m.S3Error(501, "NotImplemented")}
        self.engine = m.Transfer(self.source, self.target, source_format="fs",
                                 source_release=m.LEGACY_FS_RELEASE)
        self.assertEqual(self.capture()["buckets"]["data"]["one"]["size"], 7)


class TransportTests(unittest.TestCase):
    def test_only_literal_loopback_http_origins(self):
        for endpoint in ("http://localhost:9002", "https://127.0.0.1:9002",
                         "http://example.com", "http://127.0.0.1@evil",
                         "http://127.0.0.1/path", "http://127.0.0.1:99999",
                         "http://127.0.0.1?secret", "http://127.0.0.1:0"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(m.MigrationError):
                    m.S3(endpoint, "key", "secret")
        self.assertEqual(m.S3("http://[::1]:8333", "k", "s").endpoint, "http://[::1]:8333")

    def test_same_endpoint_rejected(self):
        with self.assertRaises(m.MigrationError):
            m.Transfer(MemoryS3(9002), MemoryS3(9002))

    def test_fs_retry_ignores_only_parent_allowed_control_bucket(self):
        source = MemoryS3(9002, {"data": {"one": obj()}})
        source.legacy_acl = True
        source.config = {"ownershipControls": m.S3Error(501, "NotImplemented"),
                         "versions": m.S3Error(501, "NotImplemented")}
        control = "open-balena-storage-" + "a" * 32
        target = MemoryS3(8333, {"data": {"one": obj(b"corrupt")},
                                control: {"identity.json": obj(b"parent-proof")}})
        engine = Harness(source, target, source_format="fs", source_release=m.LEGACY_FS_RELEASE,
                         allowed_extra_buckets=[control])
        self.assertEqual(engine.run()["objects"], 1)
        self.assertEqual(target.buckets[control]["identity.json"]["body"], b"parent-proof")
        self.assertFalse(any(call[1] == control for call in target.calls))
        target.buckets["unexpected"] = {}
        with self.assertRaisesRegex(m.MigrationError, "Unexpected target buckets"):
            engine.run()

    def test_run_cannot_exclude_source_bucket(self):
        engine = Harness(MemoryS3(9002, {"data": {}}), MemoryS3(8333),
                         allowed_extra_buckets=["data"])
        with self.assertRaisesRegex(m.MigrationError, "Cannot exclude source"):
            engine.run()

    def test_credentials_only_environment_and_output_sanitized(self):
        with patch.dict(m.os.environ, {"RCLONE_FILTER": "omit", "AWS_SESSION_TOKEN": "token",
                                      "HTTP_PROXY": "http://evil"}):
            engine = m.Transfer(MemoryS3(9002), MemoryS3(8333))
        result = Mock(returncode=1, stdout=b"secret-sensitive", stderr=b"access-sensitive")
        with patch.object(m.subprocess, "run", return_value=result) as run:
            with self.assertRaises(m.MigrationError) as error:
                engine._rclone("copy", "src:data", "dst:data")
        self.assertNotIn("sensitive", str(error.exception))
        args, kwargs = run.call_args
        self.assertNotIn("sensitive", repr(args))
        self.assertEqual(kwargs["env"]["RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY"], "secret-sensitive")
        self.assertEqual(kwargs["env"]["RCLONE_CONFIG"], m.os.devnull)
        self.assertNotIn("RCLONE_FILTER", kwargs["env"])
        self.assertNotIn("AWS_SESSION_TOKEN", kwargs["env"])
        self.assertNotIn("HTTP_PROXY", kwargs["env"])
        self.assertEqual(kwargs["timeout"], 172800)

    def test_timeout_and_execution_errors_sanitized(self):
        engine = m.Transfer(MemoryS3(9002), MemoryS3(8333))
        for error in (OSError("secret-sensitive"),
                      subprocess.TimeoutExpired("secret-sensitive", 1, b"secret-sensitive")):
            with patch.object(m.subprocess, "run", side_effect=error):
                with self.assertRaises(m.MigrationError) as caught:
                    engine._rclone("copy", "src:data", "dst:data")
                self.assertNotIn("sensitive", str(caught.exception))

    def test_sigv4_path_encoding_and_streamed_hash(self):
        client = m.S3("http://127.0.0.1:9002", "key", "secret")
        response = Mock(status=200, headers={"Content-Length": "6"})
        response.read.side_effect = [b"abc", b"def", b""]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        client._opener = Mock(open=Mock(return_value=response))
        result = client.request("GET", "data", "a /%☃", {"z": "/", "a": "x y"}, stream=True)
        self.assertEqual(result[2], {"size": 6, "sha256": hashlib.sha256(b"abcdef").hexdigest()})
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         "http://127.0.0.1:9002/data/a%20/%25%E2%98%83?a=x%20y&z=%2F")
        self.assertTrue(request.get_header("Authorization").startswith("AWS4-HMAC-SHA256 "))
        self.assertNotIn("secret", request.get_header("Authorization"))

    def test_bounded_body_success_and_oversized_response_rejected(self):
        client = m.S3("http://127.0.0.1:9002", "key", "secret")
        for payload, limit in ((b"abc", 3), (b"", 0), (b"small", 4097)):
            with self.subTest(payload=payload, limit=limit):
                response = Mock(status=200, headers={})
                response.read.side_effect = io.BytesIO(payload).read
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                client._opener = Mock(open=Mock(return_value=response))
                self.assertEqual(client.request("GET", body_limit=limit)[2], payload)
                response.read.assert_called_once_with(limit + 1)
        for headers in ({}, {"Transfer-Encoding": "chunked"}, {"Content-Length": "1"}):
            with self.subTest(headers=headers):
                response = Mock(status=200, headers=headers)
                response.read.side_effect = io.BytesIO(b"oversized-secret").read
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                client._opener = Mock(open=Mock(return_value=response))
                with self.assertRaisesRegex(m.MigrationError, "exceeds limit") as caught:
                    client.request("GET", body_limit=3)
                self.assertNotIn("secret", str(caught.exception))
                response.read.assert_called_once_with(4)

    def test_body_limit_does_not_change_stream_hash(self):
        client = m.S3("http://127.0.0.1:9002", "key", "secret")
        response = Mock(status=200, headers={})
        response.read.side_effect = [b"abc", b"def", b""]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        client._opener = Mock(open=Mock(return_value=response))
        self.assertEqual(client.request("GET", stream=True, body_limit=1)[2],
                         {"size": 6, "sha256": hashlib.sha256(b"abcdef").hexdigest()})

    def test_invalid_body_limits_fail_before_network(self):
        client = m.S3("http://127.0.0.1:9002", "key", "secret")
        client._opener = Mock()
        for limit in (-1, True, "4097", 1.5):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(m.MigrationError, "body limit"):
                    client.request("GET", body_limit=limit)
        client._opener.open.assert_not_called()

    def test_metadata_replacement_self_copy_has_exact_headers(self):
        target = MemoryS3(8333, {"data": {"a /%": obj()}})
        target.request = Mock(return_value=(200, {"ETag": '"target-etag"'}, b""))
        target.request.side_effect = [
            (200, {"ETag": '"target-etag"'}, b""),
            (200, {}, document("CopyObjectResult", [("ETag", "new-etag")]))]
        engine = m.Transfer(MemoryS3(9002), target)
        metadata = {"content-type": "text/plain", "x-amz-meta-btime": "original"}
        engine._replace_metadata("data", "a /%", {"size": 3, "metadata": metadata})
        headers = target.request.call_args.kwargs["headers"]
        self.assertEqual(headers["x-amz-copy-source"], "/data/a%20/%25")
        self.assertEqual(headers["x-amz-copy-source-if-match"], '"target-etag"')
        self.assertEqual(headers["x-amz-metadata-directive"], "REPLACE")
        self.assertEqual(headers["x-amz-meta-btime"], "original")
        self.assertNotIn("x-amz-meta-mtime", headers)

    def test_multipart_metadata_repair_ranges_completion_and_failure_abort(self):
        size = 5 * 1024 ** 3 + 7
        for fail in (False, True):
            with self.subTest(fail=fail):
                target = MemoryS3(8333)
                calls = []

                def request(method, bucket="", key="", query=None, body=b"", headers=None):
                    calls.append((method, query, body, headers))
                    if method == "HEAD":
                        return 200, {"ETag": '"stable"'}, b""
                    if method == "POST" and query == {"uploads": ""}:
                        self.assertEqual(headers, {"content-type": "text/plain"})
                        return 200, {}, document(
                            "InitiateMultipartUploadResult", [("UploadId", "owned-upload")])
                    if method == "PUT":
                        if fail:
                            raise m.MigrationError("Interrupted")
                        return 200, {}, document("CopyPartResult", [("ETag", '"part-etag"')])
                    if method == "POST":
                        self.assertEqual(len(m.xml(body).findall("Part")), 6)
                        return 200, {}, document("CompleteMultipartUploadResult")
                    if method == "DELETE":
                        self.assertEqual(query, {"uploadId": "owned-upload"})
                        return 204, {}, b""
                    raise AssertionError(method)

                target.request = request
                engine = m.Transfer(MemoryS3(9002), target)
                record = {"size": size, "metadata": {"content-type": "text/plain"}}
                if fail:
                    with self.assertRaisesRegex(m.MigrationError, "Interrupted"):
                        engine._replace_metadata("data", "one", record)
                    self.assertEqual(calls[-1][0], "DELETE")
                else:
                    engine._replace_metadata("data", "one", record)
                    parts = [call for call in calls if call[0] == "PUT"]
                    self.assertEqual(parts[0][3]["x-amz-copy-source-range"], "bytes=0-1073741823")
                    self.assertEqual(parts[-1][3]["x-amz-copy-source-range"],
                                     "bytes=5368709120-5368709126")
                    self.assertTrue(all(part[3]["x-amz-copy-source-if-match"] == '"stable"'
                                        for part in parts))
                    self.assertFalse(any(call[0] == "DELETE" for call in calls))

    def test_pinned_rustfs_large_metadata_repair_does_not_stage_payload_parts(self):
        target = MemoryS3(8333)
        target.request = Mock(side_effect=[
            (200, {"ETag": '"stable"'}, b""),
            (200, {}, document("CopyObjectResult", [("ETag", '"stable"')]))])
        engine = m.Transfer(MemoryS3(9002), target, target_release="1.0.1")
        engine._replace_metadata("data", "large", {
            "size": 1024 ** 4, "metadata": {"content-type": "application/octet-stream"}})
        self.assertEqual([call.args[0] for call in target.request.call_args_list], ["HEAD", "PUT"])
        request = target.request.call_args
        self.assertEqual(request.kwargs["headers"]["x-amz-metadata-directive"], "REPLACE")
        self.assertNotIn("uploads", repr(target.request.call_args_list))

    def test_self_copy_embedded_error_fails_closed(self):
        target = MemoryS3(8333)
        target.request = Mock(side_effect=[
            (200, {"ETag": '"stable"'}, b""),
            (200, {}, b"<Error><Code>InternalError</Code></Error>")])
        engine = m.Transfer(MemoryS3(9002), target)
        with self.assertRaises(m.MigrationError):
            engine._replace_metadata("data", "one", {"size": 1, "metadata": {}})

    def test_redirect_denied_and_http_errors_sanitized(self):
        self.assertIsNone(m._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil"))
        client = m.S3("http://127.0.0.1:9002", "key", "secret")
        error = urllib.error.HTTPError(
            client.endpoint, 403, "secret-sensitive", {},
            io.BytesIO(b"<Error><Code>secret-sensitive</Code></Error>"))
        client._opener = Mock(open=Mock(side_effect=error))
        with self.assertRaises(m.S3Error) as caught:
            client.request("GET")
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(caught.exception.code, "secret-sensitive")


if __name__ == "__main__":
    unittest.main()
