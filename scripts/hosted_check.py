#!/usr/bin/env python3
"""Launch checks for a hosted agenticml service (AWS roadmap Phase 5).

Owner-run tool, not shipped in the wheel. It needs the installed
``agenticml-lite`` package (3.0 or later) and talks to the server only through
the public protocol 2 endpoints.

    python scripts/hosted_check.py make-run --mb 300
    python scripts/hosted_check.py verify RUN_ID [RUN_ID ...]
    python scripts/hosted_check.py isolation --run RUN_ID_B

Host: ``AGENTICML_HOST`` (default https://app.agenticml.xyz). Local store:
``AGENTICML_DIR``. The API key is read from ``AGENTICML_API_KEY`` or a prompt,
never from arguments, and is never printed (only an 8 character prefix).
Exit code: 0 when every check passes, 1 when a check fails, 2 for bad usage.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

from agenticml import sync
from agenticml.auth import key_prefix, normalize_api_key
from agenticml.client import Client, _session
from agenticml.store.layout import resolve_root, valid_run_id, valid_sha
from agenticml.store.reader import RunReader, run_references

DEFAULT_HOST = "https://app.agenticml.xyz"
MAX_MB = 1500  # the free daily upload limit is 2 GB
MIB = 1024 * 1024
CHUNK = 8 * MIB
MISSING_BATCH = 10_000  # the protocol limit for POST /v2/objects/missing
TIMEOUT = 30


class Usage(Exception):
    """Bad arguments or environment; exit code 2."""


class Report:
    """Collects one line per check. The API key never reaches the output."""

    def __init__(self, secrets=()):
        self.rows = []
        self.secrets = [s for s in secrets if s]

    def add(self, name, status, detail="", run=None):
        self.rows.append({"run": run, "check": name, "status": status,
                          "detail": self.scrub(detail)})

    def scrub(self, text):
        text = str(text)
        for secret in self.secrets:
            text = text.replace(secret, key_prefix(secret))
        return text

    @property
    def failed(self):
        return any(row["status"] == "FAIL" for row in self.rows)

    def print_table(self, out=None):
        out = out or sys.stdout
        width = max([len(row["check"]) for row in self.rows] + [5])
        for row in self.rows:
            prefix = f"{row['run']}  " if row["run"] else ""
            print(f"{row['status']:<5} {prefix}{row['check']:<{width}}  {row['detail']}".rstrip(), file=out)
        print("RESULT: " + ("FAIL" if self.failed else "PASS"), file=out)

    def print_json(self, extra=None, out=None):
        out = out or sys.stdout
        doc = {**(extra or {}), "result": "FAIL" if self.failed else "PASS", "checks": self.rows}
        print(json.dumps(doc, indent=2, sort_keys=True), file=out)


def host_from_env():
    return os.environ.get("AGENTICML_HOST") or DEFAULT_HOST


def read_key():
    key = os.environ.get("AGENTICML_API_KEY")
    if not key:
        key = getpass.getpass("API key (input hidden): ")
    try:
        return normalize_api_key(key)
    except (TypeError, ValueError) as exc:
        raise Usage(f"invalid API key: {exc}") from None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# ---- make-run -------------------------------------------------------------


def make_run(args, out=None):
    """Create one finished local run. Returns (run_id, checkpoint_sha256)."""
    out = out or sys.stdout
    if not 0 < args.mb <= MAX_MB:
        raise Usage(f"--mb must be above 0 and at most {MAX_MB} (the free daily upload limit is 2 GB)")
    if args.files < 1 or args.steps < 1:
        raise Usage("--files and --steps must be at least 1")
    import agenticml

    root = resolve_root()
    total = int(args.mb * MIB)
    per_file = max(1, total // args.files)
    previous = os.getcwd()
    checkpoint_sha = None
    with tempfile.TemporaryDirectory(prefix="hosted-check-") as workdir:
        # An empty working directory, so lineage and code tracking see nothing of the caller's project.
        os.chdir(workdir)
        try:
            run = agenticml.init(project=args.project, name="launch check", dir=str(root),
                                 system_metrics=False, capture_env=False, auto_track=False, lineage=False)
            for step in range(args.steps):
                run.log({"loss": 1.0 / (step + 1), "accuracy": 1.0 - 1.0 / (step + 2)}, step=step)
            for index in range(args.files):
                path = Path(workdir) / f"file-{index:03d}.bin"
                digest = hashlib.sha256()
                with open(path, "wb") as handle:
                    remaining = per_file
                    while remaining:
                        block = os.urandom(min(remaining, CHUNK))
                        handle.write(block)
                        digest.update(block)
                        remaining -= len(block)
                name = "checkpoint" if index == 0 else f"shard-{index:03d}"
                if index == 0:
                    checkpoint_sha = digest.hexdigest()
                run.log_artifact(path, name=name, type="model" if index == 0 else "file", copy=True)
                path.unlink()
            run.summary["final_loss"] = 1.0 / args.steps
            run.finish()
        finally:
            os.chdir(previous)
    meta = json.loads((run.writer.run_dir / "meta.json").read_text())
    if meta.get("status") != "finished":
        raise RuntimeError(f"run did not finish: {meta.get('status')}")
    print(run.run_id, file=out)
    print(f"files: {args.files}  size: {per_file * args.files} bytes  steps: {args.steps}  "
          f"project: {args.project}", file=sys.stderr)
    print(f"checkpoint sha256 (for the download check): {checkpoint_sha}", file=sys.stderr)
    return run.run_id, checkpoint_sha


# ---- verify ---------------------------------------------------------------


def _local_snapshot(root, run_id):
    """What a sync of this run sends: file sizes and digests, and referenced objects."""
    reader = RunReader(root, run_id)
    effective = sync._effective(reader, reader.meta())
    result = sync.SyncResult(run_id)
    files, objects = sync._snapshot(reader, effective, result)
    local = {}
    for name, item in files.items():
        if "sha256" in item:
            local[name] = (item["size"], item["sha256"])
        else:
            local[name] = (item["size"], sync._hash_window(reader.run_dir / name, 0, item["size"]))
    return effective, local, objects, result.warnings


def _missing_objects(client, shas):
    return set(client.missing(shas))  # the client batches at the protocol limit


def verify_run(client, root, run_id, report):
    def add(name, status, detail=""):
        report.add(name, status, detail, run=run_id)

    if not valid_run_id(run_id):
        add("run id", "FAIL", "not a valid run id")
        return
    try:
        effective, local, objects, warnings = _local_snapshot(root, run_id)
    except (OSError, ValueError) as exc:
        add("local run", "FAIL", f"cannot read the local copy: {type(exc).__name__}: {exc}")
        return
    for warning in warnings:
        add("local warning", "WARN", warning)
    if effective not in ("finished", "crashed"):
        add("local status", "FAIL", f"local run is {effective}; finish it first")
    state = client.run_state(run_id)
    if state is None:
        add("server state", "FAIL", "the server does not know this run in this workspace")
        return
    add("server state", "PASS", "found")
    server_files = state.get("files") or {}
    bad = []
    for name, (size, digest) in sorted(local.items()):
        found = server_files.get(name)
        if found is None:
            bad.append(f"{name}: missing on server")
        elif found.get("size") != size or found.get("sha256") != digest:
            bad.append(f"{name}: local {size} bytes, server {found.get('size')} bytes, digests "
                       + ("equal" if found.get("sha256") == digest else "differ"))
    if bad:
        add("files", "FAIL", f"{len(bad)} of {len(local)} differ: " + "; ".join(bad[:5]))
    else:
        add("files", "PASS", f"{len(local)} files, size and sha256 match")
    extra = sorted(set(server_files) - set(local))
    if extra:
        add("extra server files", "INFO", ", ".join(extra[:10]))
    if objects:
        missing = _missing_objects(client, objects)
        if missing:
            add("objects", "FAIL", f"{len(missing)} of {len(objects)} missing: "
                + ", ".join(sorted(missing)[:3]))
        else:
            add("objects", "PASS", f"{len(objects)} referenced objects present")
    else:
        add("objects", "PASS", "no referenced objects")
    remote = state.get("remote") or {}
    if remote.get("status") == effective:
        add("remote status", "PASS", effective)
    else:
        add("remote status", "FAIL", f"local {effective}, server {remote.get('status')}")
    if valid_sha(remote.get("fingerprint")):
        add("fingerprint", "PASS", "set")
    else:
        add("fingerprint", "FAIL", "not set (the sync did not commit)")


def run_verify(args, key):
    report = Report([key])
    root = resolve_root()
    with Client(host_from_env(), key, timeout=TIMEOUT) as client:
        identity = client.whoami()
        report.add("key", "INFO", f"{key_prefix(key)} workspace {identity.get('workspace')}")
        for run_id in args.run_ids:
            try:
                verify_run(client, root, run_id, report)
            except (requests.RequestException, ValueError) as exc:
                report.add("request", "FAIL", f"{type(exc).__name__}: {exc}", run=run_id)
    return report


# ---- isolation ------------------------------------------------------------


def _expect(report, name, status, allowed, detail=""):
    ok = status in allowed
    report.add(name, "PASS" if ok else "FAIL",
               f"HTTP {status}" + (f" ({detail})" if detail else "")
               + ("" if ok else f"; expected one of {sorted(allowed)}"))


def _web_ok(response):
    if response.status_code in (401, 403, 404):
        return True
    if 300 <= response.status_code < 400:
        return urlsplit(response.headers.get("Location", "")).path.rstrip("/").endswith("/login")
    return False


def run_isolation(args, key):
    report = Report([key])
    base = Client(host_from_env(), key, timeout=TIMEOUT)
    root = resolve_root()
    try:
        identity = base.whoami()
        mine = identity.get("workspace")
        report.add("key", "INFO", f"{key_prefix(key)} workspace {mine}")
        if mine == args.slug_b:
            raise Usage(f"this key belongs to workspace {args.slug_b}; use a key from the other workspace")
        run_b = args.run
        if not valid_run_id(run_b):
            raise Usage("--run is not a valid run id")
        slug_b = args.slug_b
        report.add("workspaces", "INFO", f"probing {slug_b} with a {mine} key; read-only towards {slug_b}")

        def get(path):
            return base._request("GET", f"{base.base_url}{path}")

        _expect(report, f"GET /v2/runs/{run_b}/state", get(f"/v2/runs/{run_b}/state").status_code, {404})
        _expect(report, f"GET /v2/runs/{run_b}/lineage", get(f"/v2/runs/{run_b}/lineage").status_code, {404})
        # The server checks the run in the caller's workspace before it reads the body, and a
        # commit never creates a run, so this empty body cannot create or change anything.
        response = base._request("POST", f"{base.base_url}/v2/runs/{run_b}/commit", json={})
        if 200 <= response.status_code < 300:
            report.add(f"POST /v2/runs/{run_b}/commit", "FAIL",
                       f"HTTP {response.status_code}: the server ACCEPTED a commit for another workspace's run")
        else:
            _expect(report, f"POST /v2/runs/{run_b}/commit", response.status_code, {404})

        digests = list(dict.fromkeys(args.object or []))
        source = "--object"
        if not digests:
            source = "local copy of the run"
            try:
                digests = sorted(run_references(root, run_b)["artifacts"])
            except (OSError, ValueError):
                digests = []
        digests = digests[:MISSING_BATCH]
        if digests:
            missing = set(base.missing(digests))
            leaked = [sha for sha in digests if sha not in missing]
            if leaked:
                report.add("POST /v2/objects/missing", "FAIL",
                           f"{len(leaked)} of {len(digests)} digests are visible to this key: "
                           + ", ".join(leaked[:3]))
            else:
                report.add("POST /v2/objects/missing", "PASS",
                           f"all {len(digests)} digests ({source}) reported missing")
        else:
            report.add("POST /v2/objects/missing", "SKIP",
                       "no digests: no local copy of the run with artifacts; pass --object SHA")

        anonymous = _session()
        authed = _session()
        authed.headers["Authorization"] = f"Bearer {key}"
        try:
            for path in (f"/w/{slug_b}/", f"/api/w/{slug_b}/runs"):
                url = f"{base.base_url}{path}"
                # Dashboard pages use a browser session, not a Bearer key, so a redirect to
                # /login counts as "nothing revealed" here as it does without credentials.
                for label, session in (("key as Bearer", authed), ("no credentials", anonymous)):
                    response = session.request("GET", url, allow_redirects=False, timeout=TIMEOUT)
                    good = _web_ok(response)
                    report.add(f"GET {path} ({label})", "PASS" if good else "FAIL",
                               f"HTTP {response.status_code}"
                               + ("" if good else "; expected 401, 403, 404 or a redirect to /login"))
        finally:
            anonymous.close()
            authed.close()
        report.add("browser step", "TODO",
                   f"signed in as the {mine} owner, open /w/{slug_b}/ and /w/{slug_b}/runs/{run_b}; "
                   "both must show 'not found'")
    finally:
        base.close()
    return report


# ---- command line ---------------------------------------------------------


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the result as JSON instead of a table")
    parser = argparse.ArgumentParser(
        prog="hosted_check.py",
        description="Launch checks for a hosted agenticml service. Host from AGENTICML_HOST, "
                    "store from AGENTICML_DIR, key from AGENTICML_API_KEY or a prompt.")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make-run", parents=[common],
                          help="create one finished local run with random artifacts and two metrics")
    make.add_argument("--mb", type=float, default=300.0, help=f"artifact size in MB (default 300, max {MAX_MB})")
    make.add_argument("--files", type=int, default=30, help="number of artifact files (default 30)")
    make.add_argument("--steps", type=int, default=2000, help="metric steps for two metrics (default 2000)")
    make.add_argument("--project", default="launch-check", help="project name (default launch-check)")
    check = sub.add_parser("verify", parents=[common],
                           help="compare local runs with the server: files, objects, status, fingerprint")
    check.add_argument("run_ids", metavar="RUN_ID", nargs="+")
    iso = sub.add_parser("isolation", parents=[common],
                         help="probe another workspace's run with this workspace's key; nothing may be revealed")
    iso.add_argument("--run", required=True, metavar="RUN_ID_B", help="a run id in the other workspace")
    iso.add_argument("--object", action="append", metavar="SHA",
                     help="an object digest from the other workspace (repeatable; default: the local copy's artifacts)")
    iso.add_argument("--slug-b", default="customer-two", help="the other workspace's slug (default customer-two)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "make-run":
            run_id, checkpoint = make_run(args, out=sys.stderr if args.json else None)
            if args.json:
                print(json.dumps({"run_id": run_id, "checkpoint_sha256": checkpoint}))
            return 0
        if args.command == "isolation":
            for sha in args.object or []:
                if not valid_sha(sha):
                    raise Usage(f"--object {sha!r} is not a lowercase sha256 digest")
        key = read_key()
        report = run_verify(args, key) if args.command == "verify" else run_isolation(args, key)
    except Usage as exc:
        print(f"hosted_check.py: {exc}", file=sys.stderr)
        return 2
    except (requests.RequestException, ValueError) as exc:
        print(f"hosted_check.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        report.print_json({"command": args.command, "host": host_from_env()})
    else:
        report.print_table()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
