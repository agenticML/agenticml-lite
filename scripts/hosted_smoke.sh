#!/usr/bin/env bash
# Local only. Uses an isolated temporary database; never point this at production.
set -euo pipefail
set +x
# All fixtures are disposable and run with dummy credentials.
repo_dir=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_dir"
PYTHON=${PYTHON:-"$repo_dir/testbed/.venv/bin/python"}
export AGENTICML_TEST_DATABASE_URL=${AGENTICML_TEST_DATABASE_URL:-postgresql://postgres:pw@127.0.0.1:55432/postgres}
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 AWS_EC2_METADATA_DISABLED=true
unset AGENTICML_SERVER_DATABASE_URL AGENTICML_SERVER_KEY AGENTICML_HOST AGENTICML_API_KEY
for command in docker curl "$PYTHON"; do command -v "$command" >/dev/null; done
temp_dir=$(mktemp -d)
suffix=$("$PYTHON" -c 'import secrets; print(secrets.token_hex(6))')
name="agml-hosted-smoke-$suffix"
moto_pid=""
pg_started=false
s3_started=false
db_created=false
cleanup() {
    status=$?
    trap - EXIT
    if (( status != 0 )); then docker logs "$name" >&2 2>/dev/null || true; fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    if [[ "$s3_started" == true ]]; then docker rm -f "$name-s3" >/dev/null 2>&1 || true; fi
    if [[ -n "$moto_pid" ]]; then kill "$moto_pid" 2>/dev/null || true; wait "$moto_pid" 2>/dev/null || true; fi
    if [[ "$db_created" == true ]]; then
        "$PYTHON" - <<'CLEAN'
import os, psycopg
from psycopg import sql
with psycopg.connect(os.environ['AGENTICML_TEST_DATABASE_URL'], autocommit=True) as c:
    c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(os.environ['SMOKE_DB'])))
CLEAN
    fi
    if [[ "$pg_started" == true ]]; then docker rm -f "$name-pg" >/dev/null 2>&1 || true; fi
    rm -rf "$temp_dir"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
# Start local Postgres only when the configured disposable server is unavailable.
if ! "$PYTHON" - <<'PROBE'
import os, psycopg
with psycopg.connect(os.environ['AGENTICML_TEST_DATABASE_URL'], connect_timeout=3): pass
PROBE
then
    if [[ "$AGENTICML_TEST_DATABASE_URL" != postgresql://postgres:pw@127.0.0.1:55432/postgres ]]; then
        echo 'Configured Postgres unavailable; start it and rerun' >&2; exit 1
    fi
    docker run -d --name "$name-pg" -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=pw postgres:16 >/dev/null
    pg_started=true
    for attempt in {1..30}; do
        if docker exec "$name-pg" pg_isready -U postgres >/dev/null; then break; fi
        sleep 1
    done
fi
export SMOKE_DB="agml_smoke_$suffix"
"$PYTHON" - <<'DATABASE'
import os, psycopg
from psycopg import sql
with psycopg.connect(os.environ['AGENTICML_TEST_DATABASE_URL'], autocommit=True) as c:
    c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(os.environ['SMOKE_DB'])))
DATABASE
db_created=true
# Keep URL passwordless and supply PGPASSWORD just like the ECS task.
eval "$("$PYTHON" - <<'URL'
import os, shlex
from urllib.parse import urlsplit, urlunsplit, unquote
u=urlsplit(os.environ['AGENTICML_TEST_DATABASE_URL'])
print('export PGPASSWORD='+shlex.quote(unquote(u.password or '')))
netloc=(u.username or 'postgres')+'@'+u.hostname+':'+str(u.port or 5432)
print('export AGENTICML_SERVER_DATABASE_URL='+shlex.quote(urlunsplit((u.scheme,netloc,'/'+os.environ['SMOKE_DB'],u.query,''))))
URL
)"
s3_port=${SMOKE_S3_PORT:-5009}
if [[ "${SMOKE_S3_BACKEND:-auto}" == host ]] || { [[ "${SMOKE_S3_BACKEND:-auto}" == auto ]] \
        && "$PYTHON" -c 'import flask, flask_cors, moto.server' >/dev/null 2>&1; }; then
    "$PYTHON" -m moto.server -H 127.0.0.1 -p "$s3_port" >"$temp_dir/moto.log" 2>&1 &
    moto_pid=$!
else
    # moto[server] is not installed in the host venv: run moto in a container on the host network.
    docker run -d --name "$name-s3" --network host --entrypoint sh \
        python:3.12.12-slim-bookworm -c \
        "pip install --no-cache-dir 'moto[server]==5.2.2' && exec python -m moto.server -H 127.0.0.1 -p $s3_port" >/dev/null
    s3_started=true
fi
export AGENTICML_SERVER_S3_ENDPOINT_URL="http://127.0.0.1:$s3_port"
for attempt in {1..60}; do
    if curl -s --max-time 2 -o /dev/null "$AGENTICML_SERVER_S3_ENDPOINT_URL/"; then break; fi
    sleep 1
done
"$PYTHON" - <<'BUCKET'
import os,boto3
boto3.client('s3',endpoint_url=os.environ['AGENTICML_SERVER_S3_ENDPOINT_URL']).create_bucket(Bucket='hosted-smoke')
BUCKET
# Linux host networking gives the container the same loopback Postgres/moto endpoints.
docker build -f deploy/aws/Dockerfile -t agenticml-hosted-smoke .
base_url=http://127.0.0.1:8080
docker run -d --name "$name" --network host \
    -e AGENTICML_SERVER_DATABASE_URL -e PGPASSWORD \
    -e AGENTICML_SERVER_OBJECTS=s3://hosted-smoke/pfx \
    -e AGENTICML_SERVER_S3_ENDPOINT_URL -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
    -e AWS_DEFAULT_REGION -e AWS_EC2_METADATA_DISABLED \
    -e AGENTICML_SERVER_PUBLIC_URL="$base_url" \
    -e AGENTICML_SERVER_OPEN_SIGNUP=false agenticml-hosted-smoke >/dev/null
healthy=false
for attempt in {1..60}; do
    if [[ "$(docker inspect --format '{{.State.Running}}' "$name")" != true ]]; then echo 'Hosted container exited before health check' >&2; exit 1; fi
    if curl -sf "$base_url/healthz" >/dev/null; then healthy=true; break; fi
    sleep 1
done
[[ "$healthy" == true ]] || { echo 'Hosted health check timed out' >&2; exit 1; }
docker exec "$name" agenticml server accounts add smoke-owner --server-admin --data /data
docker exec "$name" agenticml server workspaces create hosted-smoke --owner smoke-owner --data /data
admin_key=$(docker exec "$name" agenticml server keys create --name admin --scope admin --user smoke-owner --workspace hosted-smoke --data /data)
sync_key=$(docker exec "$name" agenticml server keys create --name sync --scope sync --user smoke-owner --workspace hosted-smoke --data /data)
export AGENTICML_DIR="$temp_dir/local" AGENTICML_CONFIG_DIR="$temp_dir/config"
unset AGENTICML_HOST AGENTICML_API_KEY
"$PYTHON" - "$temp_dir" <<'PY'
import json
from pathlib import Path
import sys

import agenticml

temp = Path(sys.argv[1])
run = agenticml.init(project="smoke", name="server smoke", system_metrics=False,
                     capture_env=False, auto_track=False, lineage=False)
for step in range(3):
    run.log({"loss": 1.0 / (step + 1), "accuracy": step / 3.0}, step=step)
# A text artifact produces an artifact event through the SDK.
text = temp / "message.txt"
text.write_text("server smoke event\n")
run.log_artifact(text, name="message", copy=True)
run.finish()
directory = run.writer.run_dir
meta = json.loads((directory / "meta.json").read_text())
assert meta["status"] == "finished", meta["status"]
assert (directory / "events.jsonl").stat().st_size > 0
names = ["meta.json", "events.jsonl", "media/index.json"]
names += [path.relative_to(directory).as_posix() for path in (directory / "scalars").iterdir()
          if path.is_file()]
sizes = {name: (directory / name).stat().st_size for name in names if (directory / name).is_file()}
(temp / "expected.json").write_text(json.dumps({"run_id": run.run_id, "sizes": sizes}))
PY
run_id=$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["run_id"])' "$temp_dir/expected.json")
printf '%s\n' "$sync_key" | "$PYTHON" -m agenticml login --host "$base_url"
"$PYTHON" -m agenticml sync --all
printf 'Authorization: Bearer %s\n' "$sync_key" | \
    curl --silent --show-error --fail --max-time 10 --header @- \
    "$base_url/v2/runs/$run_id/state" >"$temp_dir/state.json"
"$PYTHON" - "$temp_dir" <<'PY'
import json
from pathlib import Path
import sys

temp = Path(sys.argv[1])
expected = json.loads((temp / "expected.json").read_text())["sizes"]
state = json.loads((temp / "state.json").read_text())
actual = {name: entry["size"] for name, entry in state["files"].items()}
assert actual == expected, (actual, expected)
PY

printf '%s' "$admin_key" | \
    "$PYTHON" -c 'import json, sys; print(json.dumps({"key": sys.stdin.read()}))' | \
    curl --silent --show-error --fail --max-time 10 \
    --header 'Content-Type: application/json' --header 'X-AgenticML-Request: 1' --header "Origin: $base_url" \
    --cookie-jar "$temp_dir/cookies" --data-binary @- "$base_url/login" > /dev/null
curl --silent --show-error --fail --max-time 10 --cookie "$temp_dir/cookies" \
    "$base_url/api/w/hosted-smoke/runs" >"$temp_dir/runs.json"
"$PYTHON" - "$temp_dir/runs.json" "$run_id" <<'PY'
import json
import sys

assert sys.argv[2] in {run["run_id"] for run in json.load(open(sys.argv[1]))}
PY
status=$(printf '%s' "$sync_key" | \
    "$PYTHON" -c 'import json, sys; print(json.dumps({"key": sys.stdin.read()}))' | \
    curl --silent --show-error --max-time 10 --output /dev/null --write-out '%{http_code}' \
    --header 'Content-Type: application/json' --header 'X-AgenticML-Request: 1' --header "Origin: $base_url" \
    --data-binary @- "$base_url/login")
[[ "$status" == 403 ]] || { echo "smoke: sync-key login returned $status, expected 403" >&2; exit 1; }

# Confirm object bytes actually reached the hosted S3 backend.
"$PYTHON" - <<'OBJECTS'
import os,boto3
s3=boto3.client('s3',endpoint_url=os.environ['AGENTICML_SERVER_S3_ENDPOINT_URL'])
assert s3.list_objects_v2(Bucket='hosted-smoke',Prefix='pfx/').get('KeyCount',0)>0
OBJECTS
docker kill --signal TERM "$name" >/dev/null
deadline=$((SECONDS + 30))
while [[ "$(docker inspect --format '{{.State.Running}}' "$name")" == true ]]; do
    if (( SECONDS >= deadline )); then echo 'Shutdown exceeded 30 seconds' >&2; exit 1; fi
    sleep 1
done
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$name")" == 0 ]]
echo 'hosted smoke: OK (Postgres, S3 objects, sync, dashboard, SIGTERM)'
