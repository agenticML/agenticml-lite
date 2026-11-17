#!/usr/bin/env python3
"""Copy, into a drill bucket, the object version of each key that was current at time T.

usage: copy_versions_at.py T_ISO_UTC SRC_BUCKET DST_BUCKET [--prefix hosted/] [--dry-run]

Selection (pure function `select_versions`, unit-tested without AWS): per key, the latest
version or delete marker with LastModified <= T. If that is a delete marker, the key did not
exist at T and is skipped ("hidden"). Versions and markers newer than T are ignored. On an
exact timestamp tie the delete marker wins (S3 timestamps have one-second granularity here).

The source bucket is only read (list-object-versions and, as copy source, the versions
themselves). Every write goes to the destination, which must be an agenticml-restore-drill-
bucket; `AwsCli` refuses any write whose --bucket is not the destination. Each copy is
checked: the object's SHA-256 (the last key segment is the hex digest) must equal the stored
checksum, and the size must equal the source version's size. Keys whose last segment is not a
SHA-256 digest are copied and checked by size only (reported as "size-only").
Needs the AWS CLI (credentials from the environment or profile).
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DRILL_BUCKET_PREFIX = "agenticml-restore-drill-"
WRITE_COMMANDS = {"copy-object", "put-object", "delete-object", "delete-objects", "put-bucket-policy"}


def parse_time(value):
    """ISO-8601 with an offset or Z (or a datetime) -> aware UTC datetime."""
    if isinstance(value, datetime):
        when = value
    else:
        text = value.strip().replace("Z", "+00:00")
        # Python 3.10 cannot read some fractional-second widths: normalise to microseconds.
        match = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
        if match and match.group(2):
            text = match.group(1) + (match.group(2) + "000000")[:7] + match.group(3)
        when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError("time must carry a UTC offset, e.g. 2026-11-16T14:30:00+00:00: %r" % (value,))
    return when.astimezone(timezone.utc)


def select_versions(versions, markers, t):
    """Return (selected, hidden): the version to copy per key, and keys hidden at T.

    versions / markers: dicts with Key, VersionId, LastModified (as in list-object-versions).
    selected: {key: version dict}; hidden: sorted list of keys whose latest entry at T is a marker.
    """
    t = parse_time(t)
    latest = {}  # key -> (when, is_marker, item)
    for is_marker, items in ((False, versions or []), (True, markers or [])):
        for item in items:
            when = parse_time(item["LastModified"])
            if when > t:
                continue
            best = latest.get(item["Key"])
            # Later wins; on an exact tie the delete marker wins.
            if best is None or when > best[0] or (when == best[0] and is_marker and not best[1]):
                latest[item["Key"]] = (when, is_marker, item)
    selected = {k: v[2] for k, v in latest.items() if not v[1]}
    hidden = sorted(k for k, v in latest.items() if v[1])
    return selected, hidden


def expected_checksum(key):
    """Base64 SHA-256 implied by the key's last segment, or None if it is not a digest."""
    last = key.rsplit("/", 1)[-1]
    return base64.b64encode(bytes.fromhex(last)).decode() if SHA_RE.match(last) else None


class AwsCli:
    """Thin aws-cli wrapper that can only write to the destination bucket."""

    def __init__(self, dst, runner=None):
        self.dst = dst
        self.runner = runner or self._run

    @staticmethod
    def _run(args):
        return subprocess.run(["aws", *args], check=True, capture_output=True, text=True).stdout

    def call(self, command, *args):
        if command in WRITE_COMMANDS:
            bucket = args[args.index("--bucket") + 1] if "--bucket" in args else None
            if bucket != self.dst:
                raise RuntimeError("refusing %s: --bucket %r is not the destination bucket" % (command, bucket))
        return self.runner(["s3api", command, *args])

    def list_versions(self, bucket, prefix):
        out = self.call("list-object-versions", "--bucket", bucket, "--prefix", prefix, "--output", "json")
        listing = json.loads(out) if out.strip() else {}
        return listing.get("Versions") or [], listing.get("DeleteMarkers") or []


def copy_selected(cli, src, selected, dry_run):
    """Copy each selected version and verify it. Returns (copied, size_only)."""
    copied = size_only = 0
    for key, item in sorted(selected.items()):
        print(("would copy " if dry_run else "copy ") + key, item["VersionId"], item["Size"])
        copied += 1
        want = expected_checksum(key)
        size_only += want is None
        if dry_run:
            continue
        cli.call("copy-object", "--bucket", cli.dst, "--key", key,
                 "--copy-source", "%s/%s?versionId=%s" % (src, key, item["VersionId"]),
                 "--server-side-encryption", "AES256", "--checksum-algorithm", "SHA256")
        head = json.loads(cli.call("head-object", "--bucket", cli.dst, "--key", key,
                                   "--checksum-mode", "ENABLED", "--output", "json"))
        if head.get("ContentLength") != item["Size"]:
            raise SystemExit("INTEGRITY FAILURE %s: size %s != %s" % (key, head.get("ContentLength"), item["Size"]))
        if want is not None and head.get("ChecksumSHA256") != want:
            raise SystemExit("INTEGRITY FAILURE %s: checksum %s != %s" % (key, head.get("ChecksumSHA256"), want))
    return copied, size_only


def main(argv=None, runner=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    prefix = "hosted/"
    if "--prefix" in argv:
        at = argv.index("--prefix")
        prefix = argv[at + 1]
        del argv[at:at + 2]
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    t_text, src, dst = argv
    t = parse_time(t_text)
    if not dst.startswith(DRILL_BUCKET_PREFIX) or dst == src:
        print("refusing: the destination must be an %s bucket other than the source" % DRILL_BUCKET_PREFIX,
              file=sys.stderr)
        return 2
    cli = AwsCli(dst, runner)
    versions, markers = cli.list_versions(src, prefix)
    selected, hidden = select_versions(versions, markers, t)
    for key in hidden:
        print("hidden at T (delete marker):", key)
    copied, size_only = copy_selected(cli, src, selected, dry_run)
    print("%s %d, hidden %d, size-only %d, bytes %d, keys considered %d, T %s" % (
        "would copy" if dry_run else "copied", copied, len(hidden), size_only,
        sum(v["Size"] for v in selected.values()), len(selected) + len(hidden), t.isoformat()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
