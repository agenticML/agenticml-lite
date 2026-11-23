# Record the 3.3.1 release in the docs and roadmap

Status: plan ready (Nov 23, 2026). Supervisor plan; Sonnet codes; supervisor reviews.
Base branch: `main` at `4c060b3` (PR #97 merged).

## Goal

3.3.1 was tagged on Nov 23, 2026:

- Annotated tag `v3.3.1` on `main` at `4c060b3` (merge of
  [#97](https://github.com/agenticML/agenticml-lite/pull/97)).
- PyPI 3.3.1 from Publish run 45199473415 (the supervisor confirms the
  upload before this PR leaves draft).

#97 already bumped the version, wrote the `## 3.3.1` Releases section and
pointed the install pages at 3.3.1. What is left is what could only be done
once the tag existed: raw GitHub URLs still name `v3.3.0`, the Releases
section has no exact date, and the roadmap still calls 3.3.1 pending. This is
the 3.3.1 counterpart of `release-3-3-0-docs.md`.

## Files to touch

Raw URLs and tag examples, `v3.3.0` → `v3.3.1` (each file exists at
`v3.3.1`; check with `git cat-file -e v3.3.1:<path>`):
1. `docs/SERVER.md` lines ~84–87 (URL and the "fetched from the `v3.3.0`
   tag" sentence).
2. `deploy/systemd/README.md` lines ~8 and ~33.
3. `deploy/aws/README.md` lines ~789–792 (`hosted_check.py` from the tag).
   Do NOT touch the `agenticml-lite@bd0d499` 2.2 pin.
4. `deploy/private-network/README.md` lines ~57–59: "for version 3.3.1:"
   with `TAG=v3.3.1`.
5. `website/src/content/docs/server/install.md` lines ~92–94 and
   `website/src/content/docs/server/sync.md` line ~104.
6. `tests/test_website_facts.py`: `PUBLISHED_TAGS = ('v3.0.0', 'v3.3.0',
   'v3.3.1')`. Keep `v3.3.0`: it is still a published tag, and the 3.3.0
   Releases section links its GitHub release.

Releases page (`website/src/content/docs/releases.md`), `## 3.3.1` only:
7. Status: `**Status:** Released Nov 23, 2026 (tag `v3.3.1`). On PyPI.`
8. If, and only if, the GitHub release for `v3.3.1` exists with
   `agenticml.yaml` attached when this is built (`gh release view v3.3.1
   --repo agenticML/agenticml-lite --json assets`), add a bullet in the
   3.3.0 section's style: the
   [GitHub release](https://github.com/agenticML/agenticml-lite/releases/tag/v3.3.1)
   has the template as `agenticml.yaml`. Otherwise leave it out; the
   supervisor adds it when the owner publishes the release.

Leave the `## 3.3.0` and older sections as they are (history).

Roadmap (`docs/plans/on-prem-roadmap.md`):
9. "Maintenance fixes":
   - the lineage fix entry: status `done ([#97](…/pull/97), merged Nov 23,
     2026; released in 3.3.1, Nov 23, 2026)`;
   - the #96 entry ("Dashboard defects seen in the README demo GIF"): "ships
     in 3.3.1" → "released in 3.3.1 (Nov 23, 2026)";
   - add this package as a new entry at the end, in the section's format:
     plan link `packages/release-3-3-1-docs.md`, status `in review`.
10. Row 35.3: "ships in 3.3.1" → "released in 3.3.1 (Nov 23, 2026)".
11. Backlog intro: replace "3.3.1 is ready in #97 and waits for the owner's
    tag (see …)" with "3.3.1 was released on Nov 23, 2026."
12. Owner entry "Release 3.3.1": the tag and the PyPI upload are done, and
    the raw URLs move in this PR. Keep the entry only for what is still
    open, and say what is done in one line ("Tag `v3.3.1` and PyPI 3.3.1
    done Nov 23, 2026."):
    - GitHub release for `v3.3.1` (`release-assets.yml` attaches
      `agenticml.yaml`), unless it exists when this is built;
    - approve the hosted deploy of `4c060b3` (`deploy.yml` run waiting for
      the `hosted` environment), unless it has run when this is built
      (`gh run list --repo agenticML/agenticml-lite --workflow deploy.yml
      --limit 3`).
    If both are done, delete the entry.

Plan file `docs/plans/packages/fix-lineage-diff-cross-host-parent.md`:
13. Status line: "built and in review in #97 …" → "done: merged in
    [#97](…) on Nov 23, 2026 and released in 3.3.1 (tag `v3.3.1`)". Keep
    the rest of the paragraph.

Do not change anything else. Do not touch `website/package-lock.json`.

## Constraints

- A raw URL may only name a tag that exists on origin (`git ls-remote
  --tags origin v3.3.1`).
- House style: short past-tense sentences, dates as "Nov 23, 2026".
- No code changes outside `tests/test_website_facts.py`.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests/test_website_facts.py`, then
  the full unit suite.
- `git grep -n "v3\.3\.0"` outside `docs/plans` and
  `website/package-lock.json` returns only `PUBLISHED_TAGS` and the 3.3.0
  Releases section.
- Each new raw URL returns HTTP 200 (`curl -sfI`).
- No dashboard change, so the UI suite is not needed.

## Done when

The docs point at the `v3.3.1` tag, the Releases page dates 3.3.1, the
roadmap records the release and lists only open owner steps, and the checks
pass.
