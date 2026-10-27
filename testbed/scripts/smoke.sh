#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Ignore inherited tracking configuration, including off switches and ranks.
for key in "${!AGENTICML_@}"; do unset "$key"; done
unset RANK PMI_RANK OMPI_COMM_WORLD_RANK SLURM_PROCID LOCAL_RANK
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
export AGENTICML_DIR="$scratch/store"
export AGENTICML_CONFIG_DIR="$scratch/config"
export XDG_CONFIG_HOME="$scratch/xdg"
export HOME="$scratch/home" USERPROFILE="$scratch/home"
mkdir -p "$HOME"
# Also support invocation from the repository without activating the testbed venv.
if [[ -x .venv/bin/python ]]; then export PATH="$PWD/.venv/bin:$PATH"; fi
agenticml run --project smoke --name zc -- python -m dummytrain.train --epochs 2 --steps-per-epoch 3 --out "$scratch/zc"
python -m dummytrain.train --explicit --project smoke --name explicit --epochs 2 --steps-per-epoch 3 --out "$scratch/explicit"
agenticml ls
agenticml ls --json --project smoke | python -c '
import json, sys
rows = json.load(sys.stdin)
assert len(rows) == 2, rows
assert {r["name"] for r in rows} == {"zc", "explicit"}, rows
assert all(r["status"] == "finished" for r in rows), rows
'
agenticml du
agenticml gc --dry-run --include-unsynced
printf 'SMOKE OK\n'
