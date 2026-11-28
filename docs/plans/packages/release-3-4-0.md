# Release 3.4.0

Status: plan ready (Nov 28, 2026). Owner asked to bump the version, tag
`v3.4.0` and publish to PyPI.

## Goal

Publish everything merged to `main` since `v3.3.1` as agenticml-lite 3.4.0:
`agenticml import` (package 41, #107) plus the server changes from #101,
#104 and #105. One PR to `main`; after it merges, the annotated tag `v3.4.0`
on the merge commit starts `publish.yml`, which checks that the tag matches
`pyproject.toml` and `__init__.py` and uploads to PyPI.

## Decisions

- **One PR, raw URLs included.** Unlike 3.3.1 (bump PR, then a docs PR after
  the tag), the raw `raw.githubusercontent.com/.../v3.3.1/...` URLs move to
  `v3.4.0` in this PR, and `PUBLISHED_TAGS` gains `v3.4.0`. The tag is pushed
  minutes after the merge, so the links are broken only for that short gap.
- **Releases page says "Released Nov 28, 2026"** in this PR, since the facts
  test requires a Released status for the package version.
- The "New in 3.4 / install from source until released" notes go away in this
  PR; "New in 3.4" alone may stay as a version marker.

## Files to touch

1. `pyproject.toml` `version` and `src/agenticml/__init__.py` `__version__`:
   `3.4.0`.
2. `deploy/aws-self-hosted/agenticml.yaml` `AgenticmlVersion` default and
   `tests/test_deploy_self_hosted.py` (the `3.3.1` literals at lines ~53,
   159, 167, 168, 743): `3.4.0`.
3. Pages that say what pip installs today or the current version: 3.4.0.
   `website/src/content/docs/index.mdx`, `server/upgrade.md`,
   `hosted/quickstart.md`, `server/deploy-on-aws.md`,
   `getting-started/installation.md`, `deploy/aws-self-hosted/README.md`,
   `deploy/aws/README.md` (line ~680), `docs/PROTOCOL.md` (User-Agent
   example).
4. Raw URLs and tag mentions `v3.3.1` → `v3.4.0`: `server/sync.md`,
   `server/install.md`, `deploy/systemd/README.md`,
   `deploy/private-network/README.md` (`TAG=` and "version 3.3.1"),
   `deploy/aws/README.md` (~802, 805), `docs/SERVER.md`. Add `'v3.4.0'` to
   `PUBLISHED_TAGS` in `tests/test_website_facts.py` (keep the old ones).
5. `website/src/content/docs/releases.md`: the version table rows and the
   sentence under it say 3.4.0; a new `## 3.4.0` section above `## 3.3.1`
   with `**Status:** Released Nov 28, 2026 (tag \`v3.4.0\`). On PyPI.`, an
   upgrade note (training machines need 3.4.0 only to use `agenticml
   import`; nothing in the store format's existing files or the sync
   protocol changes; imported runs carry a new `imported_from` meta key), the
   install command, and sections:
   - **Import from other trackers** (link `/guides/import/`): TensorBoard
     event files (scalars, images, hparams), MLflow `mlruns/` and
     `mlflow.db`, W&B local runs and W&B cloud (`[wandb]` extra), CSV/JSONL,
     Lightning `CSVLogger`, Hugging Face `trainer_state.json`; source times
     kept; re-import skips; `--dry-run`, `--replace`, `--json`.
   - **Server:** `--contact-email` / `AGENTICML_SERVER_CONTACT_EMAIL`
     (Feedback / Contact us button, `off`); `--blog-url` /
     `AGENTICML_SERVER_BLOG_URL` (blog card on the Firebase sign-in page);
     the Postgres backend re-reads a rotated password from
     `AGENTICML_SERVER_DATABASE_PASSWORD_SECRET` and logs failed connects.
   Check each option name against `docs/SERVER.md` before writing it.
6. `guides/import.md` note, `reference/configuration.md` sentence, README
   line 49: drop "until it is released, install from source"; keep "new in
   3.4".
7. `getting-started/installation.md` extras table: add a `wandb` row ("W&B
   cloud import via the wandb package", `pip install "agenticml-lite[wandb]"`).
8. `docs/plans/on-prem-roadmap.md`: the 3.4 row / row 41 say released in
   3.4.0 (Nov 28, 2026); the Backlog entry "Release 3.4 with `agenticml
   import`" is removed (done), the other two import follow-ups stay. Add a
   Maintenance/owner note only if there is an existing place for owner
   steps: GitHub release `v3.4.0` and the hosted deploy approval.

Do not touch `src/agenticml_lite.egg-info/` (untracked build output) or the
historical 3.3.1 / 3.3.0 sections of `releases.md`, plan files or
`docs/plans/packages/*.md` that record past releases.

## Checks

- `grep -rn "3\.3\.1" --exclude-dir=node_modules .` shows only history
  (releases.md 3.3.1 section, plan files, roadmap history, PUBLISHED_TAGS).
- `testbed/.venv/bin/python -m pytest -q tests/test_website_facts.py
  tests/test_deploy_self_hosted.py`, then the full suite.
- Docs site: `npm run build` and `npm run check:overflow` in `website/`
  (Node 22).

## After merge (supervisor via the git agent)

1. `git tag -a v3.4.0 -m "agenticml-lite 3.4.0" <merge sha>` and push it.
2. Watch the Publish to PyPI run; confirm `pip index versions
   agenticml-lite` (or the PyPI JSON) lists 3.4.0.
3. Owner: approve the `pypi` environment if it waits, publish the GitHub
   release (release-assets attaches `agenticml.yaml`), approve the hosted
   deploy.

## Done when

`pip install agenticml-lite==3.4.0` installs a package with `agenticml
import`, and the docs say 3.4.0 with no "install from source" note.
