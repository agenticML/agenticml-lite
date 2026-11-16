#!/bin/sh
# Restore-drill wrapper: the entrypoint of the drill server task (see mk_td.py `server`).
# It is the task's entrypoint, so a run-task command override cannot skip the guards.
#
# Guards (run before anything else; exit 3 and start no server):
#   - the database host must be an agenticml-restore-drill.* RDS instance;
#   - the objects URL must name a bucket that starts with agenticml-restore-drill-;
#   - the public URL must be loopback.
#
# DRILL_MODE=check (default): server status, workspace usage, verify --deep.
# DRILL_MODE=sync: a temporary sync key on the restored database only, a new sync, an admin
#   key login per workspace, then verify --deep. Needs WS, DRILL_USER and
#   LOGIN_PAIRS="slug:admin-user slug:admin-user".
# DRILL_CHECK_ONLY=1: run the guards, print "## guards ok" and exit 0 without starting a server.
set -eu

python -I - <<'GUARD' || exit 3
import os
import sys
from urllib.parse import urlsplit

bad = []
try:
    db = urlsplit(os.environ.get("AGENTICML_SERVER_DATABASE_URL", ""))
    query = db.query.split("&") if db.query else []
    if db.scheme != "postgresql" or not (db.hostname or "").startswith("agenticml-restore-drill."):
        bad.append("database host is not an agenticml-restore-drill. instance")
    elif any(item.split("=", 1)[0] not in ("sslmode", "sslrootcert") for item in query):
        bad.append("database URL carries an option that could redirect the connection")
    objects = urlsplit(os.environ.get("AGENTICML_SERVER_OBJECTS", ""))
    if (objects.scheme != "s3" or objects.netloc != objects.hostname
            or not (objects.hostname or "").startswith("agenticml-restore-drill-")):
        bad.append("objects URL does not name an agenticml-restore-drill- bucket")
    public = urlsplit(os.environ.get("AGENTICML_SERVER_PUBLIC_URL", ""))
    if public.hostname not in ("127.0.0.1", "localhost", "::1"):
        bad.append("public URL is not loopback")
except ValueError as exc:
    bad.append("unparseable URL (%s)" % exc)
for item in bad:
    print("REFUSING: " + item)
sys.exit(3 if bad else 0)
GUARD

echo "## guards ok"
[ "${DRILL_CHECK_ONLY:-}" != 1 ] || exit 0

echo "## mount $(grep ' /data ' /proc/mounts || echo NONE)"
echo "## task_start $(date -u +%FT%TZ)"
BASE=http://127.0.0.1:8080
health() { python -c "import urllib.request; urllib.request.urlopen('$BASE/healthz', timeout=3).close()" 2>/dev/null; }

/usr/local/bin/entrypoint.sh >/tmp/server.log 2>&1 &
SRV=$!
i=0
until health; do
  i=$((i + 1))
  if [ "$i" -ge 90 ] || ! kill -0 "$SRV" 2>/dev/null; then
    echo "## server did not start"; tail -40 /tmp/server.log; exit 1
  fi
  sleep 2
done
echo "## healthy $(date -u +%FT%TZ)"
grep -iE "unreachable|error|traceback" /tmp/server.log | head -5 || true
RC=0

SUMMARY='import json, sys
r = json.load(sys.stdin)
print(json.dumps({"runs": r.get("runs"), "objects": (r.get("sizes") or {}).get("objects_count"),
  "warnings": r.get("warnings"),
  "workspaces": [{"slug": w.get("slug"), "status": w.get("status"), "members": w.get("members"),
                  "keys": w.get("keys_count"), "runs": (w.get("runs") or {}).get("total"),
                  "objects": w.get("objects"), "stored_bytes": w.get("stored_bytes")}
                 for w in r.get("workspaces", [])],
  "users": [{"workspace": u.get("workspace"), "name": u.get("name"), "role": u.get("role")}
            for u in r.get("users", [])]}, sort_keys=True))'

# A new sync into WS as DRILL_USER with a temporary sync key (restored database only).
new_sync() {
  D=/tmp/drill
  rm -rf "$D"; mkdir -p "$D"
  export AGENTICML_DIR="$D/local" AGENTICML_CONFIG_DIR="$D/config"
  name="drill-sync-$(date -u +%H%M%S)"
  key=$(agenticml server keys create --name "$name" --scope sync --user "$DRILL_USER" --workspace "$WS" --data /data 2>/dev/null) || return 1
  run=$(D="$D" python - <<'PY'
import os, time
from pathlib import Path
import agenticml
run = agenticml.init(project="restore-drill", name="drill sync", system_metrics=False,
                     capture_env=False, auto_track=False, lineage=False)
for step in range(3):
    run.log({"loss": 1.0 / (step + 1)}, step=step)
note = Path(os.environ["D"]) / "note.txt"
note.write_text("restore drill %s\n" % time.time())  # unique bytes: a NEW object in the drill bucket
run.log_artifact(note, name="note", copy=True)
run.finish()
print(run.run_id)
PY
  ) || return 1
  printf '%s\n' "$key" | agenticml login --host "$BASE" >/dev/null || return 1
  agenticml sync --all || return 1
  RUN="$run" KEY="$key" python - <<'PY' || return 1
import json, os, urllib.request
req = urllib.request.Request("http://127.0.0.1:8080/v2/runs/" + os.environ["RUN"] + "/state",
                             headers={"Authorization": "Bearer " + os.environ["KEY"]})
files = json.load(urllib.request.urlopen(req, timeout=10))["files"]
assert files and all(f["size"] > 0 for f in files.values() if f["size"] is not None), files
print("new sync verified:", os.environ["RUN"], sorted(files))
PY
  agenticml server keys revoke "$name" --workspace "$WS" --data /data >/dev/null || return 1
  echo "drill sync: OK"
}

# Log in to the dashboard API with a temporary admin key and list the workspace's runs.
login_check() {
  slug=$1; user=$2
  name="drill-login-$(date -u +%H%M%S)-$slug"
  key=$(agenticml server keys create --name "$name" --scope admin --user "$user" --workspace "$slug" --data /data 2>/dev/null) || return 1
  KEY="$key" SLUG="$slug" python - <<'PY' || { agenticml server keys revoke "$name" --workspace "$slug" --data /data >/dev/null 2>&1; return 1; }
import http.cookiejar, json, os, urllib.request
base = "http://127.0.0.1:8080"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
req = urllib.request.Request(base + "/login", data=json.dumps({"key": os.environ["KEY"]}).encode(),
                             headers={"Content-Type": "application/json", "X-AgenticML-Request": "1", "Origin": base})
opener.open(req, timeout=10).close()
runs = json.load(opener.open(base + "/api/w/%s/runs" % os.environ["SLUG"], timeout=10))
print("login ok:", os.environ["SLUG"], "runs visible:", len(runs))
PY
  agenticml server keys revoke "$name" --workspace "$slug" --data /data >/dev/null
}

case "${DRILL_MODE:-check}" in
check)
  echo "## status"; agenticml server status --json --data /data | python -c "$SUMMARY" || RC=1
  echo "## usage"; agenticml server workspaces usage --json --data /data || RC=1
  echo "## verify --deep"; agenticml server verify --deep --data /data || RC=1
  ;;
sync)
  : "${WS:?set WS}" "${DRILL_USER:?set DRILL_USER}" "${LOGIN_PAIRS:?set LOGIN_PAIRS}"
  echo "## new sync"; new_sync || RC=1
  for pair in $LOGIN_PAIRS; do
    echo "## login ${pair%%:*}"
    login_check "${pair%%:*}" "${pair#*:}" || { echo "login check failed for $pair"; RC=1; }
  done
  echo "## verify --deep (after new sync)"; agenticml server verify --deep --data /data || RC=1
  ;;
*) echo "unknown DRILL_MODE"; RC=2;;
esac

kill -TERM "$SRV" 2>/dev/null || true
wait "$SRV" || true
echo "## done rc=$RC $(date -u +%FT%TZ)"
exit "$RC"
