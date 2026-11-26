"""Versioned S3 data and evidence objects, through AWS CLI v2's credential chain."""
import base64
import json
from pathlib import Path
import re
import subprocess


class StorageError(ValueError):
    """Keep provider diagnostics, object metadata and credentials out of service logs."""


class UnexpectedSnapshot(StorageError):
    """An actual snapshot or enabled automatic snapshot source was found."""


class S3:
    def __init__(self, config):
        self.data = config["data_bucket"]
        self.evidence = config["evidence_bucket"]
        self.owner = config["aws_account"]
        self.region = config["region"]
        self.source = config["lightsail_instance"]
        if (not re.fullmatch(r"[0-9]{12}", self.owner) or self.data == self.evidence
                or not all(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", b)
                           for b in (self.data, self.evidence))
                or not re.fullmatch(r"[a-z0-9-]+", self.region)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.source)):
            raise StorageError("Invalid storage configuration")

    def call(self, service, action, *args):
        p = subprocess.run(["aws", service, action, "--region", self.region, "--no-cli-pager",
                            "--no-cli-auto-prompt", "--output", "json", *map(str, args)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
        if p.returncode:
            raise StorageError("Provider operation failed: " + action)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def bucket(self, action, bucket, *args):
        return self.call("s3api", action, "--bucket", bucket, "--expected-bucket-owner", self.owner, *args)

    def ready(self):
        for bucket in (self.data, self.evidence):
            if self.bucket("get-bucket-versioning", bucket).get("Status") != "Enabled":
                raise StorageError("Enabled bucket versioning is required")
            flags = self.bucket("get-public-access-block", bucket)["PublicAccessBlockConfiguration"]
            if not all(flags.get(k) is True for k in
                       ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
                raise StorageError("Private bucket configuration required")

    def versions(self, bucket, prefix):
        # With JSON output AWS CLI merges every page for Versions AND DeleteMarkers.
        data = self.bucket("list-object-versions", bucket, "--prefix", prefix)
        if data.get("NextToken") or data.get("IsTruncated"):
            raise StorageError("Incomplete object version inventory")
        records = {}
        for kind in ("Versions", "DeleteMarkers"):
            for item in data.get(kind, []):
                key, version = item["Key"], item["VersionId"]
                if not key.startswith(prefix) or not version or (key, version) in records:
                    raise StorageError("Unverifiable object version inventory")
                records[key, version] = kind
        return records

    def uploads_clear(self, prefix):
        data = self.bucket("list-multipart-uploads", self.data, "--prefix", prefix)
        if data.get("Uploads") or data.get("IsTruncated") or data.get("NextToken"):
            raise StorageError("Unverified or incomplete multipart upload inventory")

    def heartbeat(self, event_id):
        # A separately configured CloudWatch alarm treats missing periods as failures,
        # including when the host, timer, IAM credentials or network are unavailable.
        self.call("cloudwatch", "put-metric-data", "--namespace", "Maison/ProductionBackup",
                  "--metric-name", "VerifiedDeliveryAndExpiry", "--value", "1", "--unit", "Count",
                  "--dimensions", "Event=" + event_id)

    def snapshots_clear(self):
        # No mutation permission on shared Lightsail snapshots. Unexpected copies
        # require the custodian to reconcile every affected environment.
        if self.call("sts", "get-caller-identity").get("Account") != self.owner:
            raise StorageError("Unexpected Lightsail inventory account")
        source = self.call("lightsail", "get-instance", "--instance-name", self.source)["instance"]
        manual = self.call("lightsail", "get-instance-snapshots")
        auto = self.call("lightsail", "get-auto-snapshots", "--resource-name", self.source)
        if (not source.get("arn") or source.get("name") != self.source or manual.get("nextPageToken")
                or auto.get("nextPageToken") or auto.get("resourceName") != self.source):
            raise StorageError("Incomplete Lightsail snapshot inventory")
        if (auto["autoSnapshots"] or any(a.get("name") == "AutoSnapshot" and a.get("status") != "Disabled"
                                       for a in source.get("addOns", []))
                or any(row.get("fromInstanceName") == self.source or row.get("fromInstanceArn") == source["arn"]
                       for row in manual["instanceSnapshots"])):
            raise UnexpectedSnapshot("Shared instance snapshots require separate custody reconciliation")

    def put(self, bucket, key, file, digest):
        checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        found = {v: kind for (k, v), kind in self.versions(bucket, key).items() if k == key}
        if not found:
            result = self.bucket("put-object", bucket, "--key", key, "--body", str(file),
                                 "--checksum-sha256", checksum, "--if-none-match", "*")
            version = result.get("VersionId")
        elif len(found) == 1 and next(iter(found.values())) == "Versions":
            version = next(iter(found))
        else:
            raise StorageError("Unexpected additional object versions or deletion marker")
        if not version or version == "null":
            raise StorageError("Versioned object receipt required")
        head = self.bucket("head-object", bucket, "--key", key, "--version-id", version,
                           "--checksum-mode", "ENABLED")
        if (head.get("ChecksumSHA256") != checksum or head.get("VersionId") != version
                or head.get("ContentLength") != Path(file).stat().st_size):
            raise StorageError("Off-host ciphertext or evidence checksum mismatch")
        return version

    def delete(self, key, version):
        # No unversioned delete: a delete marker alone does not erase earlier bytes.
        self.bucket("delete-object", self.data, "--key", key, "--version-id", version)
