"""Provider API contracts exercised against synthetic AWS CLI replies."""

import base64
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/linux"))
from prod_s3 import S3, StorageError


@pytest.fixture
def client():
    return S3(
        dict(
            aws_account="0" * 12,
            region="ap-northeast-2",
            lightsail_instance="fixture-host",
            data_bucket="fixture-data",
            evidence_bucket="fixture-receipts",
        )
    )


def test_s3_conditional_upload_and_version_checksum(client, tmp_path, monkeypatch):
    source = tmp_path / "fixture.cms"
    source.write_bytes(b"opaque-fixture")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
    calls = []

    def reply(api, action, *args):
        calls.append((action, args))
        assert api == "s3api" and args[:4] == ("--bucket", client.data, "--expected-bucket-owner", "0" * 12)
        if action == "list-object-versions":
            return {}
        if action == "put-object":
            assert args[-4:] == ("--checksum-sha256", checksum, "--if-none-match", "*")
            return {"VersionId": "fixture-version"}
        if action == "head-object":
            assert args[-4:] == ("--version-id", "fixture-version", "--checksum-mode", "ENABLED")
            return dict(
                VersionId="fixture-version", ContentLength=source.stat().st_size, ChecksumSHA256=checksum
            )
        raise AssertionError(action)

    monkeypatch.setattr(client, "call", reply)
    assert client.put(client.data, "fixture", source, digest) == "fixture-version"
    assert [action for action, args in calls] == ["list-object-versions", "put-object", "head-object"]


def test_s3_marks_incomplete_list_or_checksum_mismatch_unverified(client, tmp_path, monkeypatch):
    monkeypatch.setattr(client, "call", lambda *args: dict(Versions=[], IsTruncated=True))
    with pytest.raises(StorageError, match="Incomplete"):
        client.versions(client.data, "fixture")
    source = tmp_path / "fixture.cms"
    source.write_bytes(b"fixture")

    def reply(api, action, *args):
        if action == "list-object-versions":
            return {"Versions": [{"Key": "fixture", "VersionId": "v1"}]}
        assert action == "head-object"
        return {"VersionId": "v1", "ContentLength": source.stat().st_size, "ChecksumSHA256": "wrong"}

    monkeypatch.setattr(client, "call", reply)
    with pytest.raises(StorageError, match="checksum"):
        client.put(client.data, "fixture", source, hashlib.sha256(source.read_bytes()).hexdigest())


def test_expiry_uses_the_exact_version_not_an_unversioned_delete(client, monkeypatch):
    calls = []

    def reply(api, action, *args):
        if action == "delete-object":
            calls.append(args)
            return {}
        return {
            "Versions": [{"Key": "prefix/blob", "VersionId": "a"}],
            "DeleteMarkers": [{"Key": "prefix/blob", "VersionId": "b"}],
        }

    monkeypatch.setattr(client, "call", reply)
    items = client.versions(client.data, "prefix/")
    assert items == {("prefix/blob", "a"): "Versions", ("prefix/blob", "b"): "DeleteMarkers"}
    for key, version in items:
        client.delete(key, version)
    assert {args[-1] for args in calls} == {"a", "b"} and all(args[-2] == "--version-id" for args in calls)


@pytest.mark.parametrize("case", ["auto", "manual", "wrong-owner", "enabled"])
def test_snapshot_inventory_fail_closed(client, monkeypatch, case):
    def reply(api, action, *args):
        if action == "get-caller-identity":
            return {"Account": "1" * 12 if case == "wrong-owner" else client.owner}
        if action == "get-instance":
            return {
                "instance": {
                    "name": client.source,
                    "arn": "fixture",
                    "addOns": [{"name": "AutoSnapshot", "status": "Enabled"}] if case == "enabled" else [],
                }
            }
        if action == "get-auto-snapshots":
            return {"resourceName": client.source, "autoSnapshots": [{}] if case == "auto" else []}
        assert action == "get-instance-snapshots"
        return {"instanceSnapshots": [{"fromInstanceName": client.source}] if case == "manual" else []}

    monkeypatch.setattr(client, "call", reply)
    with pytest.raises(StorageError):
        client.snapshots_clear()
