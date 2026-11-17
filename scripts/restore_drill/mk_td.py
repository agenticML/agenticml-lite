#!/usr/bin/env python3
"""Build the Phase 6 restore-drill ECS task definitions from the live production one.

usage: mk_td.py KIND PROD_TD_JSON OUT_JSON [--prod]

PROD_TD_JSON is the output of `aws ecs describe-task-definition` for the hosted service
(either the whole response or just its "taskDefinition" object). It is only read: every
production identifier (database host, bucket, filesystem, access point, hostname) is taken
from it at run time, so nothing account-specific lives in this repository.

KIND and the environment variables it needs:
  inspect  DFS AP_INSPECT          read-only listing of the restored filesystem (root access point)
  dbcheck  DB_HOST                 read-only SQL counts against the restored database;
                                   with --prod it reads production instead (read-only transaction)
  server   DB_HOST TEST_B DFS AP_DATA
                                   the isolated drill server (entrypoint drill-run.sh)
  bench    DFS AP_BENCH            real-EFS benchmark on the restored filesystem; needs
                                   BENCH_COMMIT (default: the commit in the image tag)
Optional: PROD_EXTRA_IDS (space-separated extra production identifiers that must never appear,
for example the production target group ARN and DNS name), BENCH_REPS, BENCH_CPU, BENCH_MEMORY.

The result is refused (exit 2, message names the field) when it contains a production
identifier, an ECS Exec flag, a port mapping, or, for bench, a task role, environment or
secrets.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PROJECT_TAG = "agenticml-restore-drill"
DRILL_DB_PREFIX = "agenticml-restore-drill."
DRILL_BUCKET_PREFIX = "agenticml-restore-drill-"
KINDS = ("inspect", "dbcheck", "server", "bench")
LOOPBACK_URL = "http://127.0.0.1:8080"

# Runs in the bench container. The embedded script is checked against SHA before it runs.
BENCH_TEMPLATE = r"""set -u
ROOT=/data
SHA=@SHA@
REPS=@REPS@
if [ -n "${AGENTICML_SERVER_DATABASE_URL-}" ] || [ -n "${AGENTICML_SERVER_OBJECTS-}" ] || [ -n "${PGPASSWORD-}" ]; then
  echo "REFUSING: production environment present"; exit 99
fi
echo '@B64@' | base64 -d | gunzip > /tmp/bench_server.py
echo "$SHA  /tmp/bench_server.py" | sha256sum -c - || { echo "REFUSING: checksum mismatch"; exit 98; }
echo "## bench_server.py sha256 $SHA commit @COMMIT@"
B="$ROOT/benchmark-$(date +%s)"
case "$B" in "$ROOT"/benchmark-[0-9]*) ;; *) echo "bad dir $B"; exit 97;; esac
FAIL=0
trap 'rm -rf -- "$B"; echo "## cleaned $B"' EXIT
mkdir "$B" || exit 96
# SQLite refuses network filesystems, and the hosted database is not on EFS either: the server's
# database goes to local disk, one file per run. The variable is set per command, never exported.
LOCALDB=/tmp/bench-db
mkdir -p "$LOCALDB" || exit 95
echo "## host"; uname -srm; nproc; grep MemTotal /proc/meminfo; python -V
echo "## mount"; grep " $ROOT " /proc/mounts; df -h "$ROOT" | tail -1
echo "## db sqlite on local disk (/tmp), run files and objects on EFS"
for n in 1 8; do
  r=1
  while [ "$r" -le "$REPS" ]; do
    echo "## RUN clients=$n rep=$r"
    AGENTICML_SERVER_DATABASE_URL="sqlite:///$LOCALDB/c$n-r$r.db" timeout 900 python /tmp/bench_server.py --data "$B/c$n-r$r" --clients "$n"
    rc=$?; echo "## exit=$rc"; [ "$rc" -eq 0 ] || FAIL=1
    rm -rf -- "$B/c$n-r$r"
    rm -f -- "$LOCALDB/c$n-r$r.db" "$LOCALDB/c$n-r$r.db-wal" "$LOCALDB/c$n-r$r.db-shm"
    r=$((r+1))
  done
done
echo "## done fail=$FAIL"
exit "$FAIL"
"""


class GuardError(ValueError):
    """The generated task definition touches something it must not touch."""


# ---------------------------------------------------------------- production identifiers

def _env_map(container):
    return {e["name"]: e["value"] for e in container.get("environment", [])}


def prod_container(prod):
    containers = prod["containerDefinitions"]
    return next((c for c in containers if c.get("name") == "server"), containers[0])


def prod_identifiers(prod, extra=""):
    """Everything in the production definition that a drill definition must not name."""
    env = _env_map(prod_container(prod))
    ids = {}
    db_url = env.get("AGENTICML_SERVER_DATABASE_URL")
    if db_url:
        ids["production database host"] = urlsplit(db_url).hostname
    objects = env.get("AGENTICML_SERVER_OBJECTS")
    if objects:
        ids["production bucket"] = urlsplit(objects).netloc
    public = env.get("AGENTICML_SERVER_PUBLIC_URL")
    if public:
        ids["production hostname"] = urlsplit(public).hostname
    for volume in prod.get("volumes", []):
        cfg = volume.get("efsVolumeConfiguration") or {}
        if cfg.get("fileSystemId"):
            ids["production filesystem"] = cfg["fileSystemId"]
        ap = (cfg.get("authorizationConfig") or {}).get("accessPointId")
        if ap:
            ids["production access point"] = ap
    for i, value in enumerate(extra.split()):
        ids["extra production identifier %d" % (i + 1)] = value
    return {name: value for name, value in ids.items() if value}


def replace_db_host(url, host):
    """The production database URL with only the host replaced (user, db, TLS options kept)."""
    parts = urlsplit(url)
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = (userinfo + "@" if userinfo else "") + host + (":%d" % parts.port if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


# ---------------------------------------------------------------- guards

def _walk(node, path=""):
    """Yield (path, key_or_None, value) for every dict key and every leaf."""
    if isinstance(node, dict):
        for key, value in node.items():
            sub = "%s.%s" % (path, key) if path else str(key)
            yield sub, key, None
            yield from _walk(value, sub)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, "%s[%d]" % (path, i))
    else:
        yield path, None, node


def check_guards(td, kind, ids, allow_prod_db=False):
    """Raise GuardError, naming the field, if td is not a safe drill definition."""
    for path, key, value in _walk(td):
        if key is not None:
            low = key.lower()
            if "executecommand" in low or low == "initprocessenabled":
                raise GuardError("REFUSING: %s: ECS Exec is not allowed in a drill task definition" % path)
            if low in ("portmappings", "loadbalancers", "targetgrouparn"):
                raise GuardError("REFUSING: %s: no port mapping or load balancer is allowed "
                                 "(drill tasks are never reachable)" % path)
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        for name, ident in ids.items():
            if ident in text:
                if allow_prod_db and name == "production database host":
                    continue
                raise GuardError("REFUSING: %s contains the %s (%s)" % (path, name, ident))
    if kind in ("inspect", "dbcheck", "bench") and "taskRoleArn" in td:
        raise GuardError("REFUSING: taskRoleArn: the %s task must not have a task role" % kind)
    if kind == "bench":
        for index, container in enumerate(td["containerDefinitions"]):
            for field in ("environment", "secrets"):
                if container.get(field):
                    raise GuardError("REFUSING: containerDefinitions[%d].%s: the bench task must have "
                                     "no environment variables or secrets" % (index, field))
    if kind == "server":
        env = _env_map(td["containerDefinitions"][0])
        url = env.get("AGENTICML_SERVER_PUBLIC_URL", "")
        if urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise GuardError("REFUSING: containerDefinitions[0].environment AGENTICML_SERVER_PUBLIC_URL "
                             "must be loopback, got %r" % url)
        for name in env:
            if "FIREBASE" in name:
                raise GuardError("REFUSING: containerDefinitions[0].environment.%s: the drill server "
                                 "must not use Firebase" % name)


# ---------------------------------------------------------------- bench payload

def bench_commit(image, env):
    """BENCH_COMMIT, or the commit in an image tag like <sha>-arm64."""
    if env.get("BENCH_COMMIT"):
        return env["BENCH_COMMIT"]
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    sha = tag.split("-", 1)[0]
    if len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha):
        return sha
    raise ValueError("set BENCH_COMMIT: cannot read a commit from image %r" % image)


def bench_script_at(commit, repo=REPO):
    """scripts/bench_server.py as committed at `commit` (never the working tree)."""
    proc = subprocess.run(["git", "-C", repo, "show", "%s:scripts/bench_server.py" % commit],
                          capture_output=True)
    if proc.returncode != 0:
        raise ValueError("git show %s:scripts/bench_server.py failed: %s"
                         % (commit, proc.stderr.decode(errors="replace").strip()))
    return proc.stdout


def bench_command(script, commit, reps=3):
    """The shell text of the bench task: the script gzipped and base64-encoded, plus its sha256."""
    sha = hashlib.sha256(script).hexdigest()
    payload = base64.b64encode(gzip.compress(script, 9, mtime=0)).decode()
    return (BENCH_TEMPLATE.replace("@SHA@", sha).replace("@REPS@", str(int(reps)))
            .replace("@COMMIT@", commit).replace("@B64@", payload)), sha


# ---------------------------------------------------------------- builders

def _tags(step):
    return [{"key": "Project", "value": PROJECT_TAG}, {"key": "Step", "value": step}]


def _log(prod_c0, prefix):
    options = dict(prod_c0["logConfiguration"]["options"], **{"awslogs-stream-prefix": prefix})
    return {"logDriver": "awslogs", "options": options}


def _efs(fs, ap):
    return [{"name": "data", "efsVolumeConfiguration": {
        "fileSystemId": fs, "rootDirectory": "/", "transitEncryption": "ENABLED",
        "authorizationConfig": {"accessPointId": ap, "iam": "DISABLED"}}}]


INSPECT_SRC = """import os, sys
root = sys.argv[1]
print('top level:', sorted(os.listdir(root)))
files = size = 0
for d, ds, fs in os.walk(root):
    ds.sort()
    depth = d[len(root):].count('/')
    st = os.lstat(d)
    if depth <= 4:
        print('D %o %d:%d %s' % (st.st_mode & 0o7777, st.st_uid, st.st_gid, d))
    for f in sorted(fs):
        s = os.lstat(os.path.join(d, f))
        files += 1
        size += s.st_size
        if depth <= 3:
            print('F %o %d:%d %9d %s' % (s.st_mode & 0o7777, s.st_uid, s.st_gid, s.st_size, os.path.join(d, f)))
print('TOTAL files=%d bytes=%d' % (files, size))
"""


def build(kind, prod, env, prod_flag=False, script_loader=bench_script_at):
    """Return (task definition dict, allow_prod_db)."""
    c0 = prod_container(prod)
    required = {"inspect": ("DFS", "AP_INSPECT"), "dbcheck": () if prod_flag else ("DB_HOST",),
                "server": ("DB_HOST", "TEST_B", "DFS", "AP_DATA"), "bench": ("DFS", "AP_BENCH")}[kind]
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ValueError("%s needs environment variable(s): %s" % (kind, ", ".join(missing)))
    base = {"networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
            "runtimePlatform": prod["runtimePlatform"], "executionRoleArn": prod["executionRoleArn"]}
    secrets = [s for s in c0.get("secrets", []) if s["name"] == "PGPASSWORD"]
    allow_prod_db = False
    if kind == "inspect":
        td = dict(base, family="agenticml-restore-drill-inspect", cpu="256", memory="512", tags=_tags("6.1"),
                  volumes=_efs(env["DFS"], env["AP_INSPECT"]),
                  containerDefinitions=[{
                      "name": "inspect", "image": c0["image"], "essential": True,
                      "entryPoint": ["python", "-I", "-c", INSPECT_SRC, "/mnt/fs"], "command": [],
                      "mountPoints": [{"sourceVolume": "data", "containerPath": "/mnt/fs", "readOnly": True}],
                      "logConfiguration": _log(c0, "restore-drill-inspect")}])
    elif kind == "dbcheck":
        db_url = _env_map(c0)["AGENTICML_SERVER_DATABASE_URL"]
        if prod_flag:
            host, allow_prod_db = urlsplit(db_url).hostname, True
        else:
            host = env["DB_HOST"]
            if host == urlsplit(db_url).hostname:
                raise GuardError("REFUSING: DB_HOST is the production database host; "
                                 "pass --prod for a read-only check of production")
            if not host.startswith(DRILL_DB_PREFIX):
                raise GuardError("REFUSING: DB_HOST %r does not start with %s" % (host, DRILL_DB_PREFIX))
        if len(secrets) != 1:
            raise ValueError("the production task definition must carry exactly one PGPASSWORD secret")
        with open(os.path.join(HERE, "check_db.py")) as handle:
            source = handle.read()
        td = dict(base, family="agenticml-restore-drill-dbcheck" + ("-prod" if prod_flag else ""),
                  cpu="256", memory="512", tags=_tags("6.1"),
                  containerDefinitions=[{
                      "name": "check", "image": c0["image"], "essential": True,
                      "entryPoint": ["python", "-I", "-c", source], "command": [],
                      "environment": [{"name": "DRILL_DB_URL", "value": replace_db_host(db_url, host)}],
                      "secrets": secrets, "logConfiguration": _log(c0, "restore-drill-db")}])
    elif kind == "server":
        host, bucket = env["DB_HOST"], env["TEST_B"]
        if not host.startswith(DRILL_DB_PREFIX):
            raise GuardError("REFUSING: DB_HOST %r does not start with %s" % (host, DRILL_DB_PREFIX))
        if not bucket.startswith(DRILL_BUCKET_PREFIX):
            raise GuardError("REFUSING: TEST_B %r does not start with %s" % (bucket, DRILL_BUCKET_PREFIX))
        if len(secrets) != 1 or "taskRoleArn" not in prod:
            raise ValueError("the production task definition must carry one PGPASSWORD secret and a task role")
        prod_env = _env_map(c0)
        prefix = urlsplit(prod_env["AGENTICML_SERVER_OBJECTS"]).path.strip("/")
        keep = [e for e in c0.get("environment", []) if e["name"] in ("AWS_REGION", "AGENTICML_SERVER_LIMITS")]
        with open(os.path.join(HERE, "drill-run.sh")) as handle:
            wrapper = handle.read()
        td = dict(base, family="agenticml-restore-drill-server", cpu="1024", memory="2048", tags=_tags("6.1"),
                  taskRoleArn=prod["taskRoleArn"], volumes=_efs(env["DFS"], env["AP_DATA"]),
                  containerDefinitions=[{
                      "name": "server", "image": c0["image"], "essential": True, "stopTimeout": 120,
                      "entryPoint": ["sh", "-c", wrapper], "command": [],
                      "environment": keep + [
                          {"name": "AGENTICML_SERVER_PUBLIC_URL", "value": LOOPBACK_URL},
                          {"name": "AGENTICML_SERVER_OPEN_SIGNUP", "value": "false"},
                          {"name": "AGENTICML_SERVER_DATABASE_URL",
                           "value": replace_db_host(prod_env["AGENTICML_SERVER_DATABASE_URL"], host)},
                          {"name": "AGENTICML_SERVER_OBJECTS",
                           "value": "s3://%s%s" % (bucket, "/" + prefix if prefix else "")},
                          {"name": "DRILL_MODE", "value": "check"}],
                      "secrets": secrets,
                      "mountPoints": [{"sourceVolume": "data", "containerPath": "/data", "readOnly": False}],
                      "logConfiguration": _log(c0, "restore-drill")}])
    else:  # bench
        commit = bench_commit(c0["image"], env)
        script = script_loader(commit)
        command, _sha = bench_command(script, commit, int(env.get("BENCH_REPS", "3")))
        td = dict(base, family="agenticml-restore-drill-bench", cpu=env.get("BENCH_CPU", "4096"),
                  memory=env.get("BENCH_MEMORY", "8192"), tags=_tags("6.2"),
                  volumes=_efs(env["DFS"], env["AP_BENCH"]),
                  containerDefinitions=[{
                      "name": "bench", "image": c0["image"], "essential": True,
                      "entryPoint": ["sh", "-c"], "command": [command],
                      "environment": [], "secrets": [],
                      "mountPoints": [{"sourceVolume": "data", "containerPath": "/data", "readOnly": False}],
                      "logConfiguration": _log(c0, "restore-drill-bench")}])
    check_guards(td, kind, prod_identifiers(prod, env.get("PROD_EXTRA_IDS", "")), allow_prod_db)
    return td, allow_prod_db


def main(argv=None, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    prod_flag = "--prod" in argv
    args = [a for a in argv if a != "--prod"]
    if len(args) != 3 or args[0] not in KINDS or (prod_flag and args[0] != "dbcheck"):
        print(__doc__, file=sys.stderr)
        return 64
    kind, prod_path, out = args
    with open(prod_path) as handle:
        prod = json.load(handle)
    prod = prod.get("taskDefinition", prod)
    try:
        td, _ = build(kind, prod, env, prod_flag)
    except GuardError as exc:
        print(exc, file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print("mk_td.py: %s" % (exc if not isinstance(exc, KeyError) else "missing field %s" % exc),
              file=sys.stderr)
        return 1
    with open(out, "w") as handle:
        json.dump(td, handle, indent=1)
    print("%s %s %d bytes -> %s" % (kind, td["family"], len(json.dumps(td)), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
