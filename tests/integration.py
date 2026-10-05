"""Disposable Docker integration tests; not part of normal unit discovery.

Build the application image separately, then run:
    python tests/integration.py --run -v
    python tests/integration.py --run --registry -v

OB_S3_TEST_IMAGE defaults to open-balena-s3:test. OB_S3_INTEGRATION=1
also enables the suite; OB_S3_REGISTRY_INTEGRATION=1 enables the Distribution test.
OB_S3_RUN_INTEGRATION and OB_S3_TEST_REGISTRY are accepted as legacy aliases.
Only synthetic credentials and uniquely labeled Docker resources are used.
No third-party Python packages, host fixture files, or cluster are required.
"""

import datetime
import gzip
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import unittest
import urllib.parse
import uuid
import xml.etree.ElementTree as ET


ENABLED = (os.environ.get("OB_S3_INTEGRATION") == "1"
           or os.environ.get("OB_S3_RUN_INTEGRATION") == "1" or "--run" in sys.argv)
REGISTRY_ENABLED = (os.environ.get("OB_S3_REGISTRY_INTEGRATION") == "1"
                    or os.environ.get("OB_S3_TEST_REGISTRY") == "1" or "--registry" in sys.argv)
IMAGE = os.environ.get("OB_S3_TEST_IMAGE", "open-balena-s3:test")
REGISTRY_IMAGE = os.environ.get("OB_S3_TEST_REGISTRY_IMAGE", "registry:3.0.0")
TIMEOUT = int(os.environ.get("OB_S3_TEST_TIMEOUT", "120"))
BUCKETS = ("integration-images", "registry-data")
CONTROL = ".open-balena-storage"


def xml_nodes(body, name):
    return [node for node in ET.fromstring(body).iter() if node.tag.rsplit("}", 1)[-1] == name]


class S3:
    """The small SigV4 subset needed by these tests, using the standard library."""

    def __init__(self, port, access, secret):
        self.port, self.access, self.secret = port, access, secret

    def request(self, method, bucket="", key="", query=(), body=b"", headers=None):
        path = "/" + urllib.parse.quote(bucket, safe="-_.~")
        if key:
            path += "/" + urllib.parse.quote(key, safe="/-_.~")
        encoded = sorted((urllib.parse.quote(str(k), safe="-_.~"),
                          urllib.parse.quote(str(v), safe="-_.~")) for k, v in query)
        query_string = "&".join(k + "=" + v for k, v in encoded)
        date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        day, region = date[:8], "us-east-1"
        payload_hash = hashlib.sha256(body).hexdigest()
        signed = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
        signed.update({"host": f"127.0.0.1:{self.port}", "x-amz-date": date,
                       "x-amz-content-sha256": payload_hash})
        names = ";".join(sorted(signed))
        canonical_headers = "".join(k + ":" + signed[k] + "\n" for k in sorted(signed))
        canonical = "\n".join((method, path, query_string, canonical_headers, names, payload_hash))
        scope = f"{day}/{region}/s3/aws4_request"
        to_sign = "\n".join(("AWS4-HMAC-SHA256", date, scope,
                             hashlib.sha256(canonical.encode()).hexdigest()))
        signing_key = ("AWS4" + self.secret).encode()
        for value in (day, region, "s3", "aws4_request"):
            signing_key = hmac.new(signing_key, value.encode(), hashlib.sha256).digest()
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        signed["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.access}/{scope}, "
                                   f"SignedHeaders={names}, Signature={signature}")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path + ("?" + query_string if query_string else ""),
                               body=body, headers=signed)
            response = connection.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            connection.close()

    def expect(self, method, bucket="", key="", query=(), body=b"", headers=None, status=200):
        result = self.request(method, bucket, key, query, body, headers)
        allowed = (status,) if isinstance(status, int) else status
        if result[0] not in allowed:
            raise AssertionError(f"S3 {method} /{bucket}/{key}: HTTP {result[0]} {result[2][:1000]!r}")
        return result

    def buckets(self):
        return sorted(node.text for node in xml_nodes(self.expect("GET")[2], "Name"))

    def keys(self, bucket):
        return sorted(node.text for node in xml_nodes(
            self.expect("GET", bucket, query=(("list-type", "2"),))[2], "Key"))

    def tags(self, bucket, key):
        body = self.expect("GET", bucket, key, (("tagging", ""),))[2]
        tags = {}
        for tag in xml_nodes(body, "Tag"):
            fields = {child.tag.rsplit("}", 1)[-1]: child.text for child in tag}
            tags[fields["Key"]] = fields["Value"]
        return tags


@unittest.skipUnless(ENABLED, "opt in with --run or OB_S3_INTEGRATION=1")
class StorageIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_id = uuid.uuid4().hex[:12]
        cls.prefix = "ob-s3-it-" + cls.run_id
        cls.label = "com.openbalena.s3.integration=" + cls.run_id
        cls.access = "integration-" + cls.run_id
        cls.secret = "synthetic-" + uuid.uuid4().hex
        cls.image = cls._docker("image", "inspect", IMAGE)
        info = json.loads(cls.image)[0]
        cls.image_id = info["Id"]
        print(f"Integration image: {IMAGE} ({cls.image_id})", flush=True)
        cls.original_start = info["Config"].get("Entrypoint") or []
        cls.original_start += info["Config"].get("Cmd") or []
        if not cls.original_start:
            raise RuntimeError("Test image has no startup command")
        cls.network = cls.prefix + "-net"
        cls._docker("network", "create", "--label", cls.label, cls.network)

    @classmethod
    def tearDownClass(cls):
        cls._docker("network", "rm", cls.network, check=False)

    @classmethod
    def _docker(cls, *args, check=True, data=None, include_stderr=False):
        result = subprocess.run(["docker", *args], input=data, capture_output=True,
                                text=data is None, timeout=180)
        stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.decode(errors="replace")
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        if check and result.returncode:
            message = (stdout + "\n" + stderr).replace(cls.access, "[synthetic-access]")
            message = message.replace(cls.secret, "[synthetic-secret]")
            raise RuntimeError(f"docker {args[0]} exited {result.returncode}: {message[-6000:]}")
        return (stdout + stderr if include_stderr else stdout).strip()

    def setUp(self):
        self.containers, self.volumes = [], []
        self.counter = 0
        self.case = self.id().rsplit(".", 1)[-1].removeprefix("test_").replace("_", "-")
        self.volume = self._volume()

    def tearDown(self):
        for name in reversed(self.containers):
            self._docker("rm", "-f", name, check=False)
        for name in reversed(self.volumes):
            self._docker("volume", "rm", name, check=False)

    def _name(self, suffix):
        self.counter += 1
        fixed = len(self.prefix) + len(str(self.counter)) + len(suffix) + 3
        return f"{self.prefix}-{self.case[:63 - fixed]}-{self.counter}-{suffix}"

    def _volume(self):
        name = self._name("data")
        self._docker("volume", "create", "--label", self.label, name)
        self.volumes.append(name)
        return name

    def _create(self, suffix, options=(), command=(), image=None):
        name = self._name(suffix)
        self._docker("create", "--name", name, "--label", self.label,
                     "--network", self.network, *options, image or self.image_id, *command)
        self.containers.append(name)
        return name

    def _copy_text(self, name, destination, value, mode=0o644):
        payload = value.encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            member = tarfile.TarInfo(Path(destination).name)
            member.size, member.mode = len(payload), mode
            tar.addfile(member, io.BytesIO(payload))
        directory = destination.rsplit("/", 1)[0] or "/"
        self._docker("cp", "-", name + ":" + directory, data=archive.getvalue())

    def _port(self, name, internal):
        mapping = self._docker("port", name, str(internal) + "/tcp")
        return int(mapping.rsplit(":", 1)[-1])

    def _helper(self, code):
        name = self._name("helper")
        return self._docker("run", "--rm", "--name", name, "--label", self.label,
                            "--user", "0", "-v", self.volume + ":/export",
                            "--entrypoint", "python3", self.image_id, "-c", code)

    def _state(self):
        value = self._helper(
            "import pathlib; p=pathlib.Path('/export/.open-balena-storage/state.json'); "
            "print(p.read_text() if p.exists() else 'null')")
        return json.loads(value)

    def _inventory(self, root=""):
        return json.loads(self._helper(
            "import pathlib,hashlib,json; p=pathlib.Path('/export')/" + repr(root) + "; "
            "print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() "
            "for f in p.rglob('*') if f.is_file()}))"))

    def _logs(self, name, private=False):
        output = self._docker("logs", name, check=False, include_stderr=True)
        if private:
            summary = [line for line in output.splitlines()
                       if "Storage service failed:" in line or "INTEGRATION_GUARD_STACK" in line]
            result = subprocess.run(
                ["docker", "cp", name + ":/run/open-balena-storage/.", "-"],
                capture_output=True, timeout=15)
            if result.returncode == 0:
                try:
                    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                        for member in archive:
                            if member.isfile() and member.name.endswith(".log"):
                                stream = archive.extractfile(member)
                                output += "\nPRIVATE " + member.name + ":\n"
                                output += stream.read(128 * 1024).decode(errors="replace")[-8000:]
                except tarfile.TarError:
                    pass
            if summary:
                output += "\nSTARTUP SUMMARY:\n" + "\n".join(summary)
        return output.replace(self.access, "[synthetic-access]").replace(
            self.secret, "[synthetic-secret]")

    def _boot_failed(self, output):
        return any(marker in output for marker in (
            "s6-rc-compile: fatal:", "rc.init: fatal:", "Storage service failed:"))

    def _start(self, name):
        self._docker("start", name)

    def _stop(self, name):
        self._docker("stop", "--time", "10", name)

    def _main(self, cleanup=False, shim=None, read_only_export=False):
        mount = self.volume + ":/export" + (":ro" if read_only_export else "")
        options = ["-p", "127.0.0.1::80", "-v", mount]
        for key, value in {"S3_MINIO_ACCESS_KEY": self.access, "S3_MINIO_SECRET_KEY": self.secret,
                           "BUCKETS": ";".join(BUCKETS),
                           "CLEANUP_AFTER_MIGRATION": str(cleanup).lower()}.items():
            options += ["-e", key + "=" + value]
        if shim:
            options += ["--entrypoint", "python3"]
        name = self._create("application", options, ("/integration-start.py",) if shim else ())
        if shim:
            self._copy_text(name, "/integration-start.py", self._launcher(shim))
        self._start(name)
        return name, S3(self._port(name, 80), self.access, self.secret)

    def _launcher(self, scenario):
        # These hooks exist only in this test container's writable layer.
        pause = (
            "#!/bin/sh\n"
            'case " $* " in\n'
            '*" copy "*|*" copyto "*|*" sync "*)\n'
            "  mkdir -p /run/ob-s3-integration\n"
            "  touch /run/ob-s3-integration/rclone-paused\n"
            "  while :; do sleep 1; done;;\n"
            "esac\n"
            'exec "$0.integration-real" "$@"\n'
        )
        no_space = (
            "import os\n"
            "_real = os.statvfs\n"
            "def _limited(path):\n"
            "    result = _real(path)\n"
            "    if os.fsdecode(path).startswith('/export'):\n"
            "        values = list(result); values[3] = 0; values[4] = 0\n"
            "        return os.statvfs_result(values)\n"
            "    return result\n"
            "os.statvfs = _limited\n"
        )
        trace_backends = (
            "import http.client,json,sys,urllib.parse,xml.etree.ElementTree as ET\n"
            "_request = http.client.HTTPConnection.putrequest\n"
            "_response = http.client.HTTPConnection.getresponse\n"
            "_read = http.client.HTTPResponse.read\n"
            "def _putrequest(self,method,url,*args,**kwargs):\n"
            "    self._integration_url=url\n"
            "    self._integration_method=method\n"
            "    return _request(self,method,url,*args,**kwargs)\n"
            "def _getresponse(self,*args,**kwargs):\n"
            "    response=_response(self,*args,**kwargs)\n"
            "    if self.host == '127.0.0.1' and self.port in (9002,8333):\n"
            "        response._integration_url=getattr(self,'_integration_url','')\n"
            "        response._integration_port=self.port\n"
            "        if getattr(self,'_integration_method','') == 'HEAD':\n"
            "            fields={'content-type','content-length','cache-control','content-disposition',"
            "'content-encoding','content-language','expires'}\n"
            "            metadata={k.lower():v for k,v in response.getheaders() "
            "if k.lower() in fields or k.lower().startswith('x-amz-meta-')}\n"
            "            print('INTEGRATION_HEAD '+json.dumps("
            "{'port':self.port,'path':self._integration_url,'status':response.status,'metadata':metadata}),"
            "file=sys.stderr,flush=True)\n"
            "    return response\n"
            "def _observed_read(self,amt=None):\n"
            "    body=_read(self,amt)\n"
            "    is_acl=hasattr(self,'_integration_url') and "
            "'acl' in dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self._integration_url).query,"
            "keep_blank_values=True))\n"
            "    if hasattr(self,'_integration_url') and (self.status >= 400 or is_acl):\n"
            "            url=urllib.parse.urlsplit(self._integration_url); code=None\n"
            "            try:\n"
            "                code=next((n.text for n in ET.fromstring(body).iter() "
            "if n.tag.rsplit('}',1)[-1]=='Code'),None)\n"
            "            except ET.ParseError:\n"
            "                pass\n"
            "            print('INTEGRATION_BACKEND_HTTP '+json.dumps("
            "{'port':self._integration_port,'path':url.path,'query':url.query,"
            "'status':self.status,'code':code}),"
            "file=sys.stderr,flush=True)\n"
            "    return body\n"
            "http.client.HTTPConnection.putrequest = _putrequest\n"
            "http.client.HTTPConnection.getresponse = _getresponse\n"
            "http.client.HTTPResponse.read = _observed_read\n"
            "def _failure(frame,event,arg):\n"
            "    if event == 'exception':\n"
            "        error=arg[1]\n"
            "        if type(error).__name__ == 'MigrationError' and str(error) in "
            "('Unsupported non-private S3 ACL','Cannot inspect bucket encryption',"
            "'Target object metadata differs'):\n"
            "            print('INTEGRATION_GUARD_STACK '+json.dumps("
            "{'function':frame.f_code.co_name,'line':frame.f_lineno}),file=sys.stderr,flush=True)\n"
            "    return _failure\n"
            "def _enter(frame,event,arg):\n"
            "    return _failure if frame.f_code.co_filename.endswith('/transfer.py') else None\n"
            "sys.settrace(_enter)\n"
        )
        hook = no_space if scenario == "no-space" else trace_backends
        return (
            "import os,pathlib,shutil,site\n"
            f"scenario={scenario!r}\n"
            "if scenario == 'pause-rclone':\n"
            "    executable=pathlib.Path(shutil.which('rclone'))\n"
            "    executable.rename(str(executable)+'.integration-real')\n"
            f"    executable.write_text({pause!r}); executable.chmod(0o755)\n"
            "elif scenario in ('no-space','trace-backends'):\n"
            "    directory=pathlib.Path(site.getsitepackages()[0]); directory.mkdir(parents=True,exist_ok=True)\n"
            "    hook=directory/'sitecustomize.py'\n"
            "    previous=hook.read_text() if hook.exists() else ''\n"
            f"    hook.write_text(previous+'\\n'+{hook!r})\n"
            "    os.environ['PYTHONPATH']=str(directory)+os.pathsep+os.environ.get('PYTHONPATH','')\n"
            f"command={self.original_start!r}\n"
            "os.execvp(command[0],command)\n"
        )

    def _wait_ready(self, name, s3):
        deadline = time.monotonic() + TIMEOUT
        last = ""
        while time.monotonic() < deadline:
            try:
                visible = s3.buckets()
                state = self._state()
                expected = list(BUCKETS)
                if state and state.get("phase") == "complete" and state.get("store_id"):
                    control_bucket = "open-balena-storage-" + uuid.UUID(state["store_id"]).hex
                    self.assertEqual(s3.keys(control_bucket), ["identity.json"])
                    identity = json.loads(s3.expect("GET", control_bucket, "identity.json")[2])
                    self.assertEqual(identity, {"schema": 1, "store_id": state["store_id"]})
                    expected.append(control_bucket)
                self.assertEqual(visible, sorted(expected))
                return
            except (OSError, http.client.HTTPException, AssertionError) as error:
                last = str(error)
            output = self._logs(name)
            if self._boot_failed(output):
                self.fail(f"Application startup failed: {last}\n{self._logs(name, private=True)[-6000:]}")
            if self._docker("inspect", "--format", "{{.State.Status}}", name) == "exited":
                break
            time.sleep(0.25)
        self.fail(f"Application did not become ready: {last}\n{self._logs(name)[-6000:]}")

    def _complete(self, mode):
        state = self._state()
        self.assertIsInstance(state, dict)
        self.assertEqual(state["schema"], 1)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["mode"], mode)
        self.assertIsInstance(state["cleaned"], bool)
        self.assertEqual(uuid.UUID(state["store_id"]).hex, state["store_id"])
        self.assertIsInstance(state["source_entries"], list)
        self.assertIs(state.get("verified"), True)
        self.assertIsInstance(state["report"], dict)
        return state

    def _fixture(self, mode, extra_large=False):
        if mode == "fs":
            format_value = {"version": "1", "format": "fs", "id": str(uuid.uuid4()), "fs": {"version": "2"}}
            self._helper(
                "import pathlib,json; p=pathlib.Path('/export/.minio.sys'); p.mkdir(); "
                "(p/'format.json').write_text(" + repr(json.dumps(format_value)) + ")")
        binary = "/usr/local/bin/minio-fs" if mode == "fs" else "/usr/local/bin/minio-xl"
        source = self._create(
            "fixture", ["--entrypoint", binary, "--user", "0", "-p", "127.0.0.1::9000",
                        "-v", self.volume + ":/export", "-e", "MINIO_ROOT_USER=" + self.access,
                        "-e", "MINIO_ROOT_PASSWORD=" + self.secret],
            ("server", "/export", "--address", ":9000", "--console-address", ":9001"))
        self._start(source)
        s3 = S3(self._port(source, 9000), self.access, self.secret)
        deadline = time.monotonic() + 45
        while True:
            try:
                s3.buckets()
                break
            except (OSError, http.client.HTTPException, AssertionError):
                if time.monotonic() > deadline:
                    self.fail("Fixture MinIO did not start:\n" + self._logs(source)[-5000:])
                time.sleep(0.2)
        objects = {
            "small.txt": b"synthetic migration integration fixture\n",
            "nested/large.bin": bytes(range(256)) * 8192,
            "multipart.bin": b"M" * (5 * 1024 * 1024) + b"N" * (1024 * 1024 + 17),
        }
        if extra_large:
            objects["large-interruption.bin"] = bytes(range(256)) * (128 * 1024)
        headers = {
            "Content-Type": "application/octet-stream", "Cache-Control": "max-age=123",
            "Content-Disposition": 'attachment; filename="synthetic.bin"',
            "Content-Encoding": "identity", "Content-Language": "en",
            "x-amz-meta-fixture": "synthetic", "x-amz-meta-purpose": "integration",
            "x-amz-tagging": "fixture=synthetic&purpose=integration",
        }
        for bucket in BUCKETS:
            s3.expect("PUT", bucket, status=200)
            for key, body in objects.items():
                if key != "multipart.bin":
                    object_headers = dict(headers)
                    if key == "small.txt":
                        object_headers.update({"x-amz-meta-btime": "intentional-source-btime",
                                               "x-amz-meta-mtime": "intentional-source-mtime"})
                    s3.expect("PUT", bucket, key, body=body, headers=object_headers)
                    continue
                upload = xml_nodes(s3.expect("POST", bucket, key, (("uploads", ""),),
                                            headers=headers)[2], "UploadId")[0].text
                parts = []
                for number, part in enumerate((body[:5 * 1024 * 1024], body[5 * 1024 * 1024:]), 1):
                    etag = s3.expect("PUT", bucket, key,
                                     (("partNumber", number), ("uploadId", upload)), body=part)[1]["etag"]
                    parts.append(f"<Part><PartNumber>{number}</PartNumber><ETag>{etag}</ETag></Part>")
                completion = ("<CompleteMultipartUpload>" + "".join(parts) +
                              "</CompleteMultipartUpload>").encode()
                result = s3.expect("POST", bucket, key, (("uploadId", upload),), body=completion)[2]
                self.assertFalse(xml_nodes(result, "Error"), result)
        self._verify(s3, objects)
        self._stop(source)
        original = self._inventory()
        actual_format = json.loads(self._helper(
            "import pathlib; print(pathlib.Path('/export/.minio.sys/format.json').read_text())"))
        self.assertEqual(actual_format["format"], mode)
        return objects, original

    def _verify(self, s3, objects):
        for bucket in BUCKETS:
            self.assertEqual(s3.keys(bucket), sorted(objects))
            for key, expected in objects.items():
                head = s3.expect("HEAD", bucket, key)[1]
                self.assertEqual(int(head["content-length"]), len(expected))
                for field, value in {
                    "content-type": "application/octet-stream", "cache-control": "max-age=123",
                    "content-disposition": 'attachment; filename="synthetic.bin"',
                    "content-encoding": "identity", "content-language": "en",
                    "x-amz-meta-fixture": "synthetic", "x-amz-meta-purpose": "integration",
                }.items():
                    self.assertEqual(head.get(field), value, (bucket, key, field))
                expected_metadata = {"x-amz-meta-fixture": "synthetic", "x-amz-meta-purpose": "integration"}
                if key == "small.txt":
                    expected_metadata.update({"x-amz-meta-btime": "intentional-source-btime",
                                              "x-amz-meta-mtime": "intentional-source-mtime"})
                self.assertEqual({k: v for k, v in head.items() if k.startswith("x-amz-meta-")},
                                 expected_metadata, (bucket, key, "exact user metadata"))
                body = s3.expect("GET", bucket, key)[2]
                self.assertEqual(hashlib.sha256(body).digest(), hashlib.sha256(expected).digest())
                self.assertEqual(s3.tags(bucket, key), {"fixture": "synthetic", "purpose": "integration"})

    def _source_unchanged(self, original):
        staged = self._inventory(CONTROL + "/minio-source")
        # The real MinIO reader updates its scanner tracker, not object data or fs.json.
        relevant = {key: value for key, value in original.items()
                    if key.startswith(tuple(bucket + "/" for bucket in BUCKETS))
                    or (key.startswith(".minio.sys/buckets/")
                        and key != ".minio.sys/buckets/.tracker.bin")
                    or key == ".minio.sys/format.json"}
        self.assertTrue(relevant)
        for key, digest in relevant.items():
            self.assertEqual(staged.get(key), digest, "Source changed: " + key)
        self.assertFalse(any("preverification-denied" in key for key in staged))

    def _assert_gated(self, s3):
        try:
            status = s3.request("PUT", BUCKETS[0], "preverification-denied.txt",
                                body=b"must never become public source data")[0]
        except (OSError, http.client.HTTPException):
            return
        self.assertNotIn(status, (200, 201, 202, 204), "Public writes were accepted before verification")

    def _wait_failure(self, name, s3, words):
        deadline = time.monotonic() + min(TIMEOUT, 45)
        while time.monotonic() < deadline:
            self._assert_gated(s3)
            output = self._logs(name)
            if any(word in output.lower() for word in words):
                return
            if self._boot_failed(output):
                self.fail("Unexpected startup failure:\n" + self._logs(name, private=True)[-5000:])
            if self._docker("inspect", "--format", "{{.State.Status}}", name) == "exited":
                self.fail("Container exited without the expected error:\n" + output[-5000:])
            time.sleep(0.3)
        self.fail("Expected startup failure was not reported:\n" + self._logs(name)[-6000:])

    def test_fresh_boot_restart_and_target_loss(self):
        name, s3 = self._main()
        self._wait_ready(name, s3)
        state = self._complete("fresh")
        s3.expect("PUT", BUCKETS[0], "restart.txt", body=b"fresh persistent object")
        connection = http.client.HTTPConnection("127.0.0.1", s3.port, timeout=10)
        try:
            connection.request("GET", f"/{BUCKETS[0]}/restart.txt")
            response = connection.getresponse()
            self.assertEqual(response.status, 403, "Default target must not expose unsigned object reads")
            response.read()
        finally:
            connection.close()
        self._stop(name)
        again, s3 = self._main()
        self._wait_ready(again, s3)
        self.assertEqual(s3.expect("GET", BUCKETS[0], "restart.txt")[2], b"fresh persistent object")
        self.assertEqual(self._complete("fresh")["store_id"], state["store_id"])
        self._stop(again)
        self._helper(
            "import pathlib,shutil; p=pathlib.Path('/export'); "
            "[(shutil.rmtree(f) if f.is_dir() else f.unlink()) for f in p.iterdir() "
            "if f.name != '.open-balena-storage']")
        lost, s3 = self._main()
        self._wait_failure(lost, s3, ("identity", "missing", "lost", "backend", "format"))
        self._assert_gated(s3)

    def test_fs_copy_retention_and_deferred_cleanup(self):
        objects, original = self._fixture("fs")
        name, s3 = self._main()
        self._wait_ready(name, s3)
        self._verify(s3, objects)
        state = self._complete("fs")
        self.assertFalse(state["cleaned"])
        self._source_unchanged(original)
        self._stop(name)
        cleanup, s3 = self._main(cleanup=True)
        self._wait_ready(cleanup, s3)
        self._verify(s3, objects)
        self.assertTrue(self._complete("fs")["cleaned"])
        self.assertFalse(self._inventory(CONTROL + "/minio-source"))
        self._stop(cleanup)
        for setting in (True, False):
            name, s3 = self._main(cleanup=setting)
            self._wait_ready(name, s3)
            self._verify(s3, objects)
            self.assertTrue(self._complete("fs")["cleaned"])
            self.assertFalse(self._inventory(CONTROL + "/minio-source"))
            self._stop(name)

    def test_completed_metadata_loss_fails_closed_without_reinitializing(self):
        name, s3 = self._main()
        self._wait_ready(name, s3)
        state = self._complete("fresh")
        s3.expect("PUT", BUCKETS[0], "must-survive.txt", body=b"preserve completed payload")
        self._stop(name)
        before = self._inventory()
        self.assertIn(".rustfs.sys/format.json", before)
        self._helper(
            "import pathlib; pathlib.Path('/export/.rustfs.sys/format.json').unlink()")
        failed, s3 = self._main(cleanup=True)
        self._wait_failure(failed, s3, ("metadata", "format", "missing"))
        self._assert_gated(s3)
        after = self._inventory()
        self.assertNotIn(".rustfs.sys/format.json", after, "Completed backend was silently reinitialized")
        for key, digest in before.items():
            if key.startswith(BUCKETS[0] + "/"):
                self.assertEqual(after.get(key), digest, key)
        self.assertEqual(self._state()["store_id"], state["store_id"])

    def test_fs_interruption_public_gate_and_resume(self):
        objects, original = self._fixture("fs", extra_large=True)
        paused, s3 = self._main(shim="pause-rclone")
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            self._assert_gated(s3)
            output = self._docker("exec", paused, "python3", "-c",
                                  "import pathlib; print(pathlib.Path('/run/ob-s3-integration/rclone-paused').exists())",
                                  check=False)
            if output == "True":
                break
            logs = self._logs(paused)
            if self._boot_failed(logs):
                self.fail("Application failed before the test pause:\n" + self._logs(paused, private=True)[-6000:])
            time.sleep(0.25)
        else:
            self.fail("Test-only rclone pause was not reached:\n" + self._logs(paused)[-6000:])
        state = self._state()
        self.assertNotEqual(state.get("phase"), "complete")
        self._source_unchanged(original)
        self._docker("kill", "--signal", "KILL", paused)
        self._docker("rm", "-f", paused)
        resumed, s3 = self._main()
        self._wait_ready(resumed, s3)
        self._verify(s3, objects)
        self._complete("fs")
        self._source_unchanged(original)
        s3.expect("HEAD", BUCKETS[0], "preverification-denied.txt", status=404)

    def test_fs_insufficient_space_fails_closed(self):
        _, original = self._fixture("fs")
        name, s3 = self._main(shim="no-space")
        self._wait_failure(name, s3, ("insufficient", "free space", "disk space"))
        self._assert_gated(s3)
        state = self._state()
        if state:
            self.assertNotEqual(state.get("phase"), "complete")
            self.assertFalse(state.get("cleaned", False))
        root = self._inventory()
        staged = self._inventory(CONTROL + "/minio-source")
        for key, value in original.items():
            if key.startswith(tuple(bucket + "/" for bucket in BUCKETS)) or key == ".minio.sys/format.json":
                self.assertEqual(root.get(key, staged.get(key)), value, key)

    def test_native_xl_mutation_restart_cleanup_never_removes_shared_source(self):
        objects, original = self._fixture("xl-single")
        name, s3 = self._main(cleanup=True)
        self._wait_ready(name, s3)
        self._verify(s3, objects)
        self._complete("xl-single")
        replacement = bytes(range(256)) * 8193 + b"RustFS overwrite"
        s3.expect("PUT", BUCKETS[0], "small.txt", body=replacement,
                  headers={"Content-Type": "text/plain", "x-amz-meta-writer": "rustfs",
                           "x-amz-tagging": "writer=rustfs"})
        s3.expect("DELETE", BUCKETS[0], "nested/large.bin", status=204)
        self._stop(name)
        for setting in (True, False):
            name, s3 = self._main(cleanup=setting)
            self._wait_ready(name, s3)
            self.assertEqual(s3.expect("GET", BUCKETS[0], "small.txt")[2], replacement)
            self.assertEqual(s3.expect("HEAD", BUCKETS[0], "small.txt")[1]["x-amz-meta-writer"], "rustfs")
            self.assertEqual(s3.tags(BUCKETS[0], "small.txt"), {"writer": "rustfs"})
            s3.expect("HEAD", BUCKETS[0], "nested/large.bin", status=404)
            self.assertEqual(s3.expect("GET", BUCKETS[0], "multipart.bin")[2], objects["multipart.bin"])
            inventory = self._inventory()
            self.assertEqual(inventory.get(".minio.sys/format.json"), original[".minio.sys/format.json"])
            self.assertIn("multipart.bin/xl.meta", [key.removeprefix(BUCKETS[0] + "/")
                                                  for key in inventory if key.startswith(BUCKETS[0] + "/")])
            self._complete("xl-single")
            self._stop(name)

    def _invalid_layout(self, value):
        self._helper("import pathlib; p=pathlib.Path('/export/.minio.sys'); p.mkdir(); "
                     "(p/'format.json').write_bytes(" + repr(value) + ")")
        before = self._inventory()
        name, s3 = self._main()
        self._wait_failure(name, s3, ("unsupported", "invalid", "corrupt", "format", "only verified"))
        self._assert_gated(s3)
        after = self._inventory()
        self.assertEqual(after.get(".minio.sys/format.json"), before[".minio.sys/format.json"])
        self.assertFalse((self._state() or {}).get("cleaned", False))

    def test_unsupported_layout_fails_closed(self):
        self._invalid_layout(json.dumps({"version": "1", "format": "unsupported-synthetic",
                                         "id": str(uuid.uuid4())}).encode())

    def test_corrupt_layout_fails_closed(self):
        self._invalid_layout(b"{ deliberately broken JSON")

    def test_read_only_unsupported_volume_fails_closed(self):
        value = json.dumps({"version": "1", "format": "unsupported-synthetic",
                            "id": str(uuid.uuid4())}).encode()
        self._helper("import pathlib; p=pathlib.Path('/export/.minio.sys'); p.mkdir(); "
                     "(p/'format.json').write_bytes(" + repr(value) + ")")
        before = self._inventory()
        name, s3 = self._main(read_only_export=True)
        self._wait_failure(name, s3, ("read-only", "read only", "unsupported", "only verified"))
        self._assert_gated(s3)
        self.assertEqual(self._inventory(), before)

    @unittest.skipUnless(REGISTRY_ENABLED, "opt in with --registry or OB_S3_REGISTRY_INTEGRATION=1")
    def test_actual_distribution_v3_upload_manifest_delete_and_gc(self):
        main, s3 = self._main()
        self._wait_ready(main, s3)
        self._complete("fresh")
        try:
            self._docker("image", "inspect", REGISTRY_IMAGE)
        except RuntimeError:
            self._docker("pull", REGISTRY_IMAGE)
        config = {
            "version": "0.1", "log": {"level": "info"},
            "storage": {"s3": {"region": "us-east-1", "regionendpoint": f"http://{main}:80",
                                "bucket": "registry-data", "accesskey": self.access,
                                "secretkey": self.secret, "rootdirectory": "data",
                                "secure": False, "v4auth": True, "forcepathstyle": True,
                                "chunksize": 5242880},
                        "delete": {"enabled": True}, "redirect": {"disable": True},
                        "maintenance": {"uploadpurging": {"enabled": False}}},
            "http": {"addr": ":5000", "secret": "synthetic-http-" + self.run_id},
        }
        registry = self._create("registry", ("-p", "127.0.0.1::5000"), image=REGISTRY_IMAGE)
        self._copy_text(registry, "/etc/distribution/config.yml", json.dumps(config))
        self._start(registry)
        port = self._port(registry, 5000)

        def request(method, location, body=b"", headers=None, expected=200):
            parsed = urllib.parse.urlsplit(location)
            if parsed.netloc:
                self.assertEqual(parsed.hostname, "127.0.0.1")
                self.assertEqual(parsed.port, port)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request(method, path, body, headers or {})
                response = connection.getresponse()
                result = (response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
                self.assertEqual(result[0], expected, (method, path, result[2][:1000]))
                return result
            finally:
                connection.close()

        def ready():
            deadline = time.monotonic() + 45
            while True:
                try:
                    request("GET", "/v2/")
                    return
                except (OSError, http.client.HTTPException, AssertionError):
                    if time.monotonic() >= deadline:
                        self.fail("Registry not ready:\n" + self._logs(registry)[-5000:])
                    time.sleep(0.2)

        ready()
        repo = "fixture/main"

        def upload(body, resume=False):
            nonlocal port
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            _, headers, _ = request("POST", f"/v2/{repo}/blobs/uploads/", expected=202)
            location = headers["location"]
            if resume:
                offset = 0
                for number, piece in enumerate((body[:5 * 1024 * 1024 + 17], body[5 * 1024 * 1024 + 17:])):
                    _, headers, _ = request("PATCH", location, piece,
                                            {"Content-Type": "application/octet-stream",
                                             "Content-Range": f"{offset}-{offset + len(piece) - 1}"}, 202)
                    offset += len(piece)
                    location = headers["location"]
                    if number == 0:
                        self._docker("restart", "--time", "5", registry)
                        port = self._port(registry, 5000)
                        parsed = urllib.parse.urlsplit(location)
                        if parsed.netloc:
                            location = urllib.parse.urlunsplit(
                                parsed._replace(netloc=f"127.0.0.1:{port}"))
                        ready()
                        _, headers, _ = request("GET", location, expected=204)
                        self.assertEqual(headers["range"], f"0-{offset - 1}")
                        location = headers.get("location", location)
                body = b""
            location += ("&" if "?" in location else "?") + urllib.parse.urlencode({"digest": digest})
            request("PUT", location, body, {"Content-Type": "application/octet-stream"}, 201)
            return digest

        payload = bytes(range(256)) * (32 * 1024) + b"registry fixture"
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            member = tarfile.TarInfo("synthetic.bin")
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
        uncompressed = stream.getvalue()
        blob = gzip.compress(uncompressed, compresslevel=0, mtime=0)
        digest = upload(blob, resume=True)
        _, headers, received = request("GET", f"/v2/{repo}/blobs/{digest}")
        self.assertEqual(received, blob)
        self.assertEqual(headers["docker-content-digest"], "sha256:" + hashlib.sha256(received).hexdigest())
        _, headers, received = request("GET", f"/v2/{repo}/blobs/{digest}",
                                       headers={"Range": "bytes=12345-23456"}, expected=206)
        self.assertEqual(received, blob[12345:23457])
        self.assertEqual(headers["content-range"], f"bytes 12345-23456/{len(blob)}")
        image_config = json.dumps({
            "architecture": "amd64", "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(uncompressed).hexdigest()]},
        }, separators=(",", ":")).encode()
        config_digest = upload(image_config)
        media = "application/vnd.docker.distribution.manifest.v2+json"
        manifest = json.dumps({
            "schemaVersion": 2, "mediaType": media,
            "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                       "digest": config_digest, "size": len(image_config)},
            "layers": [{"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                        "digest": digest, "size": len(blob)}]}, separators=(",", ":")).encode()
        _, headers, _ = request("PUT", f"/v2/{repo}/manifests/latest", manifest,
                                 {"Content-Type": media}, 201)
        manifest_digest = headers["docker-content-digest"]
        self.assertEqual(request("GET", f"/v2/{repo}/manifests/latest", headers={"Accept": media})[2], manifest)
        self.assertEqual(json.loads(request("GET", "/v2/_catalog?n=1")[2])["repositories"], [repo])
        self.assertEqual(json.loads(request("GET", f"/v2/{repo}/tags/list")[2])["tags"], ["latest"])
        old_blob = b"obsolete synthetic registry layer"
        old_digest = upload(old_blob)
        obsolete = json.loads(manifest)
        obsolete["layers"][0].update(digest=old_digest, size=len(old_blob))
        _, headers, _ = request("PUT", f"/v2/{repo}/manifests/obsolete",
                                 json.dumps(obsolete, separators=(",", ":")).encode(),
                                 {"Content-Type": media}, 201)
        deleted_digest = headers["docker-content-digest"]
        request("DELETE", f"/v2/{repo}/manifests/{deleted_digest}", expected=202)
        request("GET", f"/v2/{repo}/manifests/{deleted_digest}", headers={"Accept": media}, expected=404)
        orphan = upload(b"unreferenced integration blob")
        self._stop(registry)
        before = s3.keys("registry-data")
        for dry_run in (True, False):
            command = ["garbage-collect", "--delete-untagged"]
            if dry_run:
                command += ["--dry-run"]
            command += ["/etc/distribution/config.yml"]
            gc = self._create("gc", command=command, image=REGISTRY_IMAGE)
            self._copy_text(gc, "/etc/distribution/config.yml", json.dumps(config))
            self._docker("start", "-a", gc)
            self.assertEqual(self._docker("inspect", "--format", "{{.State.ExitCode}}", gc), "0",
                             self._logs(gc))
            if dry_run:
                self.assertEqual(s3.keys("registry-data"), before)
        self._start(registry)
        port = self._port(registry, 5000)
        ready()
        self.assertEqual(request("GET", f"/v2/{repo}/manifests/{manifest_digest}",
                                 headers={"Accept": media})[2], manifest)
        self.assertEqual(request("GET", f"/v2/{repo}/blobs/{digest}")[2], blob)
        request("GET", f"/v2/{repo}/blobs/{old_digest}", expected=404)
        request("GET", f"/v2/{repo}/blobs/{orphan}", expected=404)


if __name__ == "__main__":
    sys.argv = [value for value in sys.argv if value not in ("--run", "--registry")]
    unittest.main()
