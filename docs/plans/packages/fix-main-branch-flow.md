# Fix: `main` is the working branch

Status: in review (Nov 20, 2026).

## Goal

On Nov 20, 2026 the owner made `main` the GitHub default branch and asked
that every pull request target `main` ("lets make all of the PR's to
main"). `dev` was merged into `main` and is retired as the integration
branch. Everything that still treats `dev` as the working branch must move
to `main`, above all the hosted deploy trigger: if it stays on `dev`, merges
to `main` never reach app.agenticml.xyz.

Nothing changes for AWS: the deploy role trusts the `hosted` environment
(`...:environment:hosted`), not a branch, and the `hosted` environment
already allows `main`, `dev` and `v*` tags. The `prod-release` ruleset
already protects `main` (no deletion, no force-push).

## Files

- `.github/workflows/deploy.yml`: `on.push.branches: [main]`; the comment
  above `on:` says "A push to main".
- `.github/workflows/test.yml`: `on.push.branches: [main]` (drop `dev`).
- `.github/workflows/monitor.yml`: the comment says the default branch is
  `main`.
- `tests/test_deploy_cloudformation.py` (~line 1279): the trigger assertion
  expects `['main']`.
- `CONTRIBUTING.md`, "Branches and pull requests": open pull requests
  against `main`; releases are `v*` tags on `main`. Drop "Do not target it
  with feature work".
- `deploy/aws/README.md`: every present-tense mention of `dev` as the deploy
  branch or the default branch becomes `main`:
  - the "deploy workflow runs automatically on every push to `dev`" section
    (~line 227) and "merge to `dev`";
  - "anyone who can push to `dev` ... keep `dev` protected" becomes `main`;
  - "the default branch. That is `dev`" and the `gh workflow run deploy.yml
    --ref dev ...` commands become `--ref main`; the following line "You can
    also run against `main` or a `v*` tag" becomes "against a `v*` tag or a
    commit SHA" (no repetition);
  - "Every push to `dev` that changes the image" (~line 960), "revert on
    `dev`" (~line 976);
  - "Scheduled runs use the default branch (`dev`)" (~line 1228);
  - the other `--ref dev` commands (~lines 1291, 1316, 1334);
  - the environment setup step "Restrict permitted branches/tags to `main`,
    `dev`, and tags matching `v*`" becomes "`main` and tags matching `v*`"
    for new setups (the live environment keeps `dev` until the owner removes
    it; that is fine).
- `docs/HOSTED.md` step 5 "How deploys start": `dev` becomes `main`,
  including `--ref main` and "revert on `main`".
- `website/src/content/docs/hosted/workspaces.md` (~line 81): "install the
  client again from `dev`" becomes "upgrade the client
  (`pip install -U agenticml-lite`, 3.0 or later)". 3.0.0 on PyPI has
  per-workspace sync state.
- `docs/plans/on-prem-roadmap.md`:
  - "Maintenance fixes": add an entry for this fix (where, problem, status).
  - Backlog "Two-task rolling deploy check": `--ref dev` becomes `--ref main`
    and "(the default branch is `dev`, ...)" becomes `main`.
  - Any other open Backlog or Owner-steps line that tells someone to deploy
    or dispatch from `dev` today: change it to `main`.
- this plan file, as `docs/plans/packages/fix-main-branch-flow.md`.

## Do not change

- History: lines that record what happened (release notes in
  `website/src/content/docs/releases.md`, `hosted/status.md`, the roadmap's
  done rows such as "PR to `dev`", "Released Nov 17: `dev` merged into
  `main`"). They were true then.
- `.[dev]` extras, `/dev/...` paths, `--dev` flags: not the branch.
- `deploy/aws/cloudformation/*.yaml`: the trust policy has no branch.

## Approach

1. `git grep -n -w dev` over the repo (excluding `.[dev]`, `/dev/`,
   `dev.to`) and sort every hit into "present-tense instruction" (change)
   or "history" (keep). Report the list in the hand-back.
2. Make the changes above. Keep the wording style: short, plain sentences.
3. Run the checks.

## Edge cases

- `workflow_dispatch` only works for workflows on the default branch;
  `main` already has `deploy.yml` and `monitor.yml`, so dispatch keeps
  working.
- The hosted deploy has a required reviewer, so the first push to `main`
  that matches the paths queues a run that waits for approval; nothing
  deploys on its own.

## Tests and checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root (at
  least `tests/test_deploy_cloudformation.py`, `tests/test_website_facts.py`
  must pass; run the full suite).
- No dashboard change, so no UI run is needed.

## Done when

- `deploy.yml` and `test.yml` trigger on `main`, the test asserts it, and no
  present-tense doc tells anyone to merge to, protect, dispatch from or
  revert on `dev`.
- The full unit suite passes.
- The PR targets `main`.
