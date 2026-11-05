#!/usr/bin/env bash
set -euo pipefail
# Never expose keys when invoked by a shell with tracing enabled.
set +x

repo_dir=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-"$repo_dir/testbed/.venv/bin/python"}
for command in docker curl "$PYTHON"; do
    command -v "$command" >/dev/null || { echo "smoke: missing $command" >&2; exit 1; }
done
PYTHON=$(command -v "$PYTHON")
if [[ "$PYTHON" != /* ]]; then
    PYTHON=$(cd "$(dirname "$PYTHON")" && printf '%s/%s\n' "$PWD" "$(basename "$PYTHON")")
fi
if [[ -n "${PYTHONPATH+x}" ]]; then
    # Preserve the caller's import paths when changing to the repository root.
    export PYTHONPATH
    PYTHONPATH=$("$PYTHON" -c 'import os; print(os.pathsep.join(os.path.abspath(path) for path in os.environ["PYTHONPATH"].split(os.pathsep) if path))')
    if [[ -z "$PYTHONPATH" ]]; then
        unset PYTHONPATH
    fi
fi
cd "$repo_dir"

temp_dir=$(mktemp -d)
container=""
volume=""
cleanup() {
    status=$?
    trap - EXIT
    if [[ -n "$container" ]]; then
        if (( status != 0 )); then docker logs "$container" >&2 || true; fi
        docker rm -f -v "$container" >/dev/null 2>&1 || true
    fi
    if [[ -n "$volume" ]]; then docker volume rm "$volume" >/dev/null 2>&1 || true; fi
    rm -rf "$temp_dir"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

image=${SMOKE_IMAGE:-agenticml-server:smoke}
if [[ -z "${SMOKE_IMAGE:-}" ]]; then
    docker build -f docker/Dockerfile -t "$image" .
fi

AGENTICML_SERVER_KEY=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')
volume=$(docker volume create)
container=$(AGENTICML_SERVER_KEY="$AGENTICML_SERVER_KEY" docker run -d --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount "type=volume,source=$volume,target=/data" \
    -p 127.0.0.1::8080 --env AGENTICML_SERVER_KEY "$image")
port=$(docker port "$container" 8080/tcp)
base_url="http://127.0.0.1:${port##*:}"
deadline=$((SECONDS + 60))
until curl --silent --fail --max-time 2 "$base_url/healthz" >"$temp_dir/health.json"; do
    if [[ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]]; then
        echo "smoke: container stopped before becoming healthy" >&2
        exit 1 # The EXIT trap prints this container's docker logs.
    fi
    if (( SECONDS >= deadline )); then
        echo "smoke: health check timed out after 60s" >&2
        exit 1 # The EXIT trap prints docker logs.
    fi
    sleep 1
done
"$PYTHON" -c 'import json, sys; assert json.load(open(sys.argv[1])) == {"ok": True}' "$temp_dir/health.json"
curl --silent --show-error --fail --max-time 5 \
    --header 'Host: arbitrary-health-check.example' "$base_url/healthz" >"$temp_dir/health.json"
"$PYTHON" -c 'import json, sys; assert json.load(open(sys.argv[1])) == {"ok": True}' "$temp_dir/health.json"

sync_key=$(docker exec "$container" agenticml server keys create --name smoke --scope sync --data /data)
export AGENTICML_DIR="$temp_dir/local" AGENTICML_CONFIG_DIR="$temp_dir/config"
unset AGENTICML_HOST AGENTICML_API_KEY
"$PYTHON" - "$temp_dir" <<'PY'
import json
from pathlib import Path
import sys

import agenticml

temp = Path(sys.argv[1])
run = agenticml.init(project="smoke", name="container smoke", system_metrics=False,
                     capture_env=False, auto_track=False, lineage=False)
for step in range(3):
    run.log({"loss": 1.0 / (step + 1), "accuracy": step / 3.0}, step=step)
# A text artifact produces an artifact event through the SDK.
text = temp / "message.txt"
text.write_text("container smoke event\n")
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

printf '%s' "$AGENTICML_SERVER_KEY" | \
    "$PYTHON" -c 'import json, sys; print(json.dumps({"key": sys.stdin.read()}))' | \
    curl --silent --show-error --fail --max-time 10 \
    --header 'Content-Type: application/json' --header 'X-AgenticML-Request: 1' --header "Origin: $base_url" \
    --cookie-jar "$temp_dir/cookies" --data-binary @- "$base_url/login" > /dev/null
curl --silent --show-error --fail --max-time 10 --cookie "$temp_dir/cookies" \
    "$base_url/api/runs" >"$temp_dir/runs.json"
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

started=$SECONDS
docker stop --time 30 "$container" >/dev/null
(( SECONDS - started <= 30 )) || { echo "smoke: shutdown exceeded 30s" >&2; exit 1; }
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$container")" == 0 ]] || {
    echo "smoke: container exited nonzero" >&2; exit 1;
}
echo 'smoke: OK'
