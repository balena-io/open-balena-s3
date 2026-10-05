import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "migration"))
boot = importlib.import_module("bootstrap")


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="open-balena-s3-unit-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def fs(self):
        system = self.root / ".minio.sys"
        system.mkdir()
        boot.atomic_json(system / "format.json", {
            "version": "1", "format": "fs", "id": str(uuid.uuid4()), "fs": {"version": "2"},
        })
        bucket = self.root / "registry-data"
        bucket.mkdir()
        (bucket / "blob").write_bytes(b"unchanged source")

    def xl(self):
        drive = str(uuid.uuid4())
        system = self.root / ".minio.sys"
        system.mkdir()
        value = {"version": "1", "format": "xl-single", "id": str(uuid.uuid4()),
                 "xl": {"version": "3", "this": drive, "sets": [[drive]],
                        "distributionAlgo": "SIPMOD+PARITY"}}
        boot.atomic_json(system / "format.json", value)
        return value

    def completed_fs(self):
        self.fs()
        volume = boot.Volume(self.root)
        volume.load()
        volume.stage()
        volume.target.mkdir()
        (volume.target / "retained-target").write_bytes(b"target")
        volume.state.update(phase="complete", verified=True)
        volume.save()
        return volume

    def symlink(self, target, link):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError:
            self.skipTest("Symbolic links unavailable for this test account")

    def test_fresh_volume(self):
        volume = boot.Volume(self.root)
        state = volume.load()
        self.assertEqual(state["mode"], "fresh")
        self.assertEqual(state["phase"], "capturing")
        self.assertFalse(state["verified"])
        self.assertEqual(boot.Volume(self.root).load()["store_id"], state["store_id"])

    def test_health_lookup_never_initializes_empty_volume(self):
        with self.assertRaises(boot.MigrationError):
            boot.Volume(self.root, create=False)
        self.assertEqual(list(self.root.iterdir()), [])
        volume = boot.Volume(self.root)
        with self.assertRaises(boot.MigrationError):
            volume.load(initialize=False)
        self.assertFalse(volume.path.exists())

    def test_fs_stage_moves_bytes_without_rewriting_format(self):
        self.fs()
        original = (self.root / ".minio.sys" / "format.json").read_bytes()
        volume = boot.Volume(self.root)
        volume.load()
        volume.stage()
        self.assertFalse((self.root / ".minio.sys").exists())
        self.assertEqual((volume.source / ".minio.sys" / "format.json").read_bytes(), original)
        self.assertEqual((volume.source / "registry-data" / "blob").read_bytes(), b"unchanged source")
        self.assertEqual(volume.state["phase"], "capturing")
        volume.stage()

    def test_interrupted_staging_resumes(self):
        self.fs()
        volume = boot.Volume(self.root)
        volume.load()
        volume.source.mkdir()
        os.rename(self.root / ".minio.sys", volume.source / ".minio.sys")
        resumed = boot.Volume(self.root)
        resumed.load()
        resumed.stage()
        self.assertTrue((resumed.source / "registry-data" / "blob").exists())
        self.assertEqual(resumed.state["phase"], "capturing")

    def test_staging_collision_fails(self):
        self.fs()
        volume = boot.Volume(self.root)
        volume.load()
        volume.source.mkdir()
        (volume.source / "registry-data").mkdir()
        with self.assertRaisesRegex(boot.MigrationError, "collision"):
            volume.stage()

    def test_native_xl_not_staged(self):
        self.xl()
        volume = boot.Volume(self.root)
        volume.load()
        volume.stage()
        self.assertEqual(volume.state["mode"], "xl-single")
        self.assertTrue((self.root / ".minio.sys").is_dir())
        self.assertFalse(volume.source.exists())

    def test_unknown_nonempty_volume_rejected(self):
        (self.root / "valuable").write_bytes(b"preserve")
        with self.assertRaises(boot.MigrationError):
            boot.Volume(self.root).load()
        self.assertEqual((self.root / "valuable").read_bytes(), b"preserve")

    def test_existing_untracked_rustfs_rejected(self):
        (self.root / ".rustfs.sys").mkdir()
        with self.assertRaises(boot.MigrationError):
            boot.Volume(self.root).load()

    def test_corrupt_format_rejected(self):
        system = self.root / ".minio.sys"
        system.mkdir()
        (system / "format.json").write_text("{")
        with self.assertRaises(boot.MigrationError):
            boot.detect_format(self.root)

    def test_multi_drive_xl_rejected(self):
        value = self.xl()
        value["xl"]["sets"][0].append(str(uuid.uuid4()))
        boot.atomic_json(self.root / ".minio.sys" / "format.json", value)
        with self.assertRaises(boot.MigrationError):
            boot.detect_format(self.root)

    def test_other_xl_generation_rejected(self):
        value = self.xl()
        value["xl"]["version"] = "2"
        boot.atomic_json(self.root / ".minio.sys" / "format.json", value)
        with self.assertRaises(boot.MigrationError):
            boot.detect_format(self.root)

    def test_bad_drive_identity_rejected(self):
        value = self.xl()
        value["xl"]["this"] = "invalid"
        value["xl"]["sets"] = [["invalid"]]
        boot.atomic_json(self.root / ".minio.sys" / "format.json", value)
        with self.assertRaises(boot.MigrationError):
            boot.detect_format(self.root)

    def test_source_tree_symlink_rejected(self):
        self.fs()
        self.symlink(self.root / "registry-data" / "blob", self.root / "registry-data" / "link")
        volume = boot.Volume(self.root)
        volume.load()
        with self.assertRaises(boot.MigrationError):
            volume.stage()
        self.assertTrue((self.root / "registry-data" / "blob").exists())

    def test_reserved_directory_symlink_rejected(self):
        directory = self.root / "elsewhere"
        directory.mkdir()
        self.symlink(directory, self.root / boot.RESERVED)
        with self.assertRaises(boot.MigrationError):
            boot.Volume(self.root)

    def test_completion_record_requires_verification(self):
        volume = boot.Volume(self.root)
        volume.load()
        volume.state["phase"] = "complete"
        volume.save()
        with self.assertRaises(boot.MigrationError):
            boot.Volume(self.root).load()

    def test_state_path_traversal_rejected(self):
        self.fs()
        volume = boot.Volume(self.root)
        volume.load()
        volume.state["source_entries"] = ["../outside"]
        volume.save()
        with self.assertRaises(boot.MigrationError):
            boot.Volume(self.root).load()

    def test_untracked_partial_migration_rejected(self):
        volume = boot.Volume(self.root)
        volume.target.mkdir()
        with self.assertRaises(boot.MigrationError):
            volume.load()

    def test_default_cleanup_retains_source(self):
        volume = self.completed_fs()
        volume.cleanup(False)
        self.assertTrue(volume.source.is_dir())
        self.assertFalse(volume.state["cleaned"])

    def test_later_restart_cleanup_is_durable_and_idempotent(self):
        volume = self.completed_fs()
        resumed = boot.Volume(self.root)
        resumed.load()
        resumed.cleanup(True)
        self.assertFalse(resumed.source.exists())
        self.assertEqual((resumed.target / "retained-target").read_bytes(), b"target")
        self.assertTrue(boot.Volume(self.root).load()["cleaned"])
        resumed.cleanup(True)

    def test_cleanup_never_deletes_native_shared_payload(self):
        self.xl()
        blob = self.root / "blob"
        blob.write_bytes(b"native shared data")
        volume = boot.Volume(self.root)
        volume.load()
        volume.state.update(phase="complete", verified=True)
        volume.save()
        volume.cleanup(True)
        self.assertEqual(blob.read_bytes(), b"native shared data")
        self.assertTrue((self.root / ".minio.sys").exists())
        self.assertFalse(volume.state["cleaned"])

    def test_cleanup_requires_complete_state(self):
        self.fs()
        volume = boot.Volume(self.root)
        volume.load()
        volume.stage()
        with self.assertRaises(boot.MigrationError):
            volume.cleanup(True)
        self.assertTrue(volume.source.exists())

    def test_interrupted_cleanup_resumes(self):
        volume = self.completed_fs()
        volume.state["cleanup_started"] = True
        volume.save()
        (volume.source / "registry-data" / "blob").unlink()
        resumed = boot.Volume(self.root)
        resumed.load()
        resumed.cleanup(True)
        self.assertTrue(resumed.state["cleaned"])
        self.assertFalse(resumed.source.exists())

    def test_deleted_source_after_interrupted_cleanup_acknowledged(self):
        volume = self.completed_fs()
        volume.state["cleanup_started"] = True
        volume.save()
        shutil.rmtree(volume.source)
        resumed = boot.Volume(self.root)
        resumed.load()
        resumed.cleanup(True)
        self.assertTrue(resumed.state["cleaned"])

    def test_unexpected_source_loss_not_acknowledged_as_cleanup(self):
        volume = self.completed_fs()
        shutil.rmtree(volume.source)
        with self.assertRaises(boot.MigrationError):
            volume.cleanup(True)
        self.assertFalse(volume.state["cleaned"])

    def test_low_space_rejected(self):
        volume = boot.Volume(self.root)
        with patch.object(boot.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaisesRegex(boot.MigrationError, "2x"):
                volume.check_capacity(10)

    def test_space_estimate_credits_resumed_copy(self):
        volume = boot.Volume(self.root)
        overhead = 64 * 1024 * 1024
        with patch.object(boot.shutil, "disk_usage",
                          return_value=shutil._ntuple_diskusage(200, 100, overhead + 10)):
            volume.check_capacity(100, copied_bytes=90)
            with self.assertRaises(boot.MigrationError):
                volume.check_capacity(100)

    def test_resume_reserves_largest_replacement_at_exact_threshold(self):
        volume = boot.Volume(self.root)
        size = 1024 * 1024 * 1024
        copied = 900 * 1024 * 1024
        largest = 512 * 1024 * 1024
        required = size - copied + largest + size // 10
        with patch.object(boot.shutil, "disk_usage",
                          return_value=shutil._ntuple_diskusage(required, 0, required)):
            volume.check_capacity(size, copied_bytes=copied, largest_object=largest)
        with patch.object(boot.shutil, "disk_usage",
                          return_value=shutil._ntuple_diskusage(required, 1, required - 1)):
            with self.assertRaisesRegex(boot.MigrationError, "object-replacement"):
                volume.check_capacity(size, copied_bytes=copied, largest_object=largest)

    def test_initial_copy_does_not_reserve_second_target_payload(self):
        volume = boot.Volume(self.root)
        size = 1024 * 1024 * 1024
        required = size + size // 10
        with patch.object(boot.shutil, "disk_usage",
                          return_value=shutil._ntuple_diskusage(required, 0, required)):
            volume.check_capacity(size, largest_object=size)

    def test_invalid_largest_object_estimate_rejected(self):
        volume = boot.Volume(self.root)
        for largest in (-1, True, 101):
            with self.assertRaisesRegex(boot.MigrationError, "largest object"):
                volume.check_capacity(100, largest_object=largest)

    def test_custom_iam_rejected(self):
        path = self.root / ".minio.sys" / "config" / "iam" / "users" / "custom-user"
        path.mkdir(parents=True)
        (path / "identity.json").write_text("{}")
        with self.assertRaises(boot.MigrationError):
            boot.reject_custom_iam(self.root)

    def test_mixed_or_relabelled_fs_layout_rejected(self):
        self.fs()
        path = self.root / "registry-data" / "object" / "xl.meta"
        path.parent.mkdir()
        path.write_bytes(b"XL2 \x01\x00metadata")
        with self.assertRaises(boot.MigrationError):
            boot.reject_mixed_fs_layout(self.root)

    def test_backend_metadata_loss_rejected_before_restart(self):
        volume = self.completed_fs()
        with self.assertRaises(boot.MigrationError):
            volume.validate_backend_files()
        system = volume.target / ".rustfs.sys"
        system.mkdir()
        boot.atomic_json(system / "format.json", {"format": "xl-single"})
        volume.validate_backend_files()

    def test_replaced_cleanup_source_rejected(self):
        volume = self.completed_fs()
        replaced = volume.home / "saved-original"
        os.rename(volume.source, replaced)
        volume.source.mkdir()
        (volume.source / "do-not-delete").write_bytes(b"replacement")
        with self.assertRaises(boot.MigrationError):
            volume.cleanup(True)
        self.assertTrue((volume.source / "do-not-delete").exists())
        self.assertTrue(replaced.exists())

    def test_same_device_nested_mount_rejected(self):
        volume = self.completed_fs()
        fake_mount = "42 1 0:1 / %s rw - ext4 source rw" % (volume.source / "registry-data")
        with patch.object(boot.sys, "platform", "linux"), patch.object(
                boot.Path, "read_text", return_value=fake_mount):
            with self.assertRaisesRegex(boot.MigrationError, "mounted"):
                boot.validate_tree(volume.source, self.root)
        self.assertTrue((volume.source / "registry-data" / "blob").exists())

    def test_atomic_records_can_be_reloaded(self):
        path = self.root / "record.json"
        boot.atomic_json(path, {"value": 1})
        boot.atomic_json(path, {"value": 2})
        self.assertEqual(boot.read_json(path), {"value": 2})
        self.assertEqual([p.name for p in self.root.iterdir()], ["record.json"])

    def probe_backend(self, volume, *, exists=True, body=None):
        class Backend:
            def __init__(self):
                self.exists = exists
                self.body = body
                self.creations = 0

            def request(self, method, bucket="", key="", **kwargs):
                if method == "GET" and not bucket:
                    entries = "<Bucket><Name>%s</Name></Bucket>" % volume.bucket if self.exists else ""
                    return 200, {}, ("<ListAllMyBucketsResult><Buckets>%s</Buckets>"
                                     "</ListAllMyBucketsResult>" % entries).encode()
                if method == "GET" and not key:
                    entries = "<Contents><Key>identity.json</Key></Contents>" if self.body is not None else ""
                    return 200, {}, ("<ListBucketResult><IsTruncated>false</IsTruncated>%s"
                                     "</ListBucketResult>" % entries).encode()
                if method == "GET":
                    return 200, {}, self.body
                if not key:
                    if self.exists:
                        raise boot.S3Error(409, "BucketAlreadyOwnedByYou")
                    self.exists = True
                    self.creations += 1
                else:
                    self.body = kwargs["body"]
                return 200, {}, b""
        return Backend()

    def test_probe_resumes_after_bucket_created_before_identity(self):
        volume = boot.Volume(self.root)
        volume.load()
        backend = self.probe_backend(volume)
        volume.create_probe(backend)
        self.assertEqual(backend.creations, 0)
        self.assertEqual(backend.body, volume.identity())
        volume.create_probe(backend)
        self.assertEqual(backend.creations, 0)

    def test_probe_initialization_creates_only_owned_bucket(self):
        volume = boot.Volume(self.root)
        volume.load()
        backend = self.probe_backend(volume, exists=False)
        volume.create_probe(backend)
        self.assertEqual(backend.creations, 1)
        self.assertEqual(backend.body, volume.identity())

    def test_probe_never_overwrites_foreign_identity(self):
        volume = boot.Volume(self.root)
        volume.load()
        backend = self.probe_backend(volume, body=b"foreign")
        with self.assertRaises(boot.MigrationError):
            volume.create_probe(backend)
        self.assertEqual(backend.body, b"foreign")


class ConfigurationTests(unittest.TestCase):
    def test_cleanup_flags(self):
        for value in ("1", "true", "TRUE", " true "):
            self.assertTrue(boot.boolean(value))
        for value in ("0", "false", "FALSE"):
            self.assertFalse(boot.boolean(value))
        for value in ("", "yes", "2"):
            with self.assertRaises(boot.MigrationError):
                boot.boolean(value)

    def test_required_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(boot.MigrationError):
                boot.settings()

    def test_existing_region_and_explicit_override(self):
        with patch.dict(os.environ, {"S3_MINIO_ACCESS_KEY": "root", "S3_MINIO_SECRET_KEY": "secret",
                                     "MINIO_REGION_NAME": "eu-west-1"}, clear=True):
            self.assertEqual(boot.settings()[2], "eu-west-1")
            os.environ["S3_REGION"] = "us-west-2"
            self.assertEqual(boot.settings()[2], "us-west-2")

    def test_bucket_names_and_deduplication(self):
        self.assertEqual(boot.bucket_names("registry-data; empty-bucket;registry-data"),
                         ["registry-data", "empty-bucket"])
        for value in ("bad/bucket", "x", "../outside", "CapitalBucket"):
            with self.assertRaises(boot.MigrationError):
                boot.bucket_names(value)

    def test_manifest_size(self):
        self.assertEqual(boot.manifest_size({"schema": 1, "buckets": {
            "bucket": {"one": {"size": 10}, "two": {"size": 20}}, "empty": {},
        }}), 30)
        for value in ({}, {"schema": 1, "buckets": {"b": {"k": {"size": -1}}}},
                      {"schema": 1, "buckets": {"b": {"k": {"size": True}}}}):
            with self.assertRaises(boot.MigrationError):
                boot.manifest_size(value)

    def test_manifest_stats_include_largest_object(self):
        self.assertEqual(boot.manifest_stats({"schema": 1, "buckets": {
            "bucket": {"one": {"size": 10}, "two": {"size": 20}}, "empty": {},
        }}), (30, 20))
        self.assertEqual(boot.manifest_stats({"schema": 1, "buckets": {}}), (0, 0))


if __name__ == "__main__":
    unittest.main()
