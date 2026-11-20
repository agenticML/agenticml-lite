# Record the 3.3.0 release in the docs and roadmap

Status: plan ready (Nov 20, 2026). Supervisor plan; Sonnet codes; supervisor reviews.

## Goal

3.3.0 was released on Nov 20, 2026:

- Annotated tag `v3.3.0` on `main` at `b506493`.
- [PyPI 3.3.0](https://pypi.org/project/agenticml-lite/3.3.0/), from Publish run 44722690342.
- [GitHub release](https://github.com/agenticML/agenticml-lite/releases/tag/v3.3.0), with `agenticml.yaml` attached by Release assets run 44722866146.

3.2.0 was never tagged or published on its own. 3.3.0 contains the 3.1 and 3.2 work.

The docs and roadmap still describe 3.2.0 and 3.3.0 as unreleased, and say that `pip install` gets 3.0.0. This change makes them true and adds a test so this drift is caught. It is the 3.3 counterpart of package 29.9, which did the same for 3.0.0.

The hosted service was also moved to `main`: the `hosted` environment allows only `main` and `v*`, and app.agenticml.xyz runs `b506493`.

## Files to touch

Docs site (`website/src/content/docs/`):
1. `releases.md`
   - "Which version should I use?" table: all three rows `3.3.0 from PyPI`.
   - Paragraph under the table: "`pip install agenticml-lite` installs 3.3.0, …".
   - "A 2.2 client … Use the 3.0 client." becomes "Use 3.0 or newer." Keep the `requires client version 2.2 or newer` sentence; the facts test guards it.
   - `## 3.3.0`:
     - Status: `**Status:** Released Nov 20, 2026 (tag \`v3.3.0\`). On PyPI.`
     - Add one sentence: it includes the 3.1 and 3.2 work below, which was not published on its own.
     - Add the install block (`pip install 'agenticml-lite[system]'`), as in the 3.0.0 section.
     - Add one bullet: the GitHub release has the template as `agenticml.yaml`.
   - `## 3.2.0`:
     - Status: `**Status:** Not published on its own; included in 3.3.0.`
     - Replace "Install from PyPI once it is published:" and its code block with: "Version 3.1 was not released separately either. Install 3.3.0 to get this work."
     - Keep the feature bullets unchanged.
2. `getting-started/installation.md` line ~15: installs 3.3.0.
3. `index.mdx` line ~29: keep the history, but make the current version clear. For example: "Version 3.0.0 added workspaces and the hosted service to the self-hosted server; the current version is 3.3.0." Keep the rest of the sentence as it is.
4. `server/upgrade.md` line ~63: "This installs 3.3.0, …".
5. `hosted/quickstart.md` line ~102: "to install 3.3.0".
6. `server/install.md` lines ~92–94 and `server/sync.md` line ~104: raw unit URLs `v3.0.0` → `v3.3.0`. Also the sentence "The unit is fetched from the `v3.0.0` tag" → `v3.3.0`.
7. `server/deploy-on-aws.md`, Console step 1: "Download `agenticml.yaml` from the [latest GitHub release](https://github.com/agenticML/agenticml-lite/releases/latest/download/agenticml.yaml), or from the release for your version." This link was checked and works. Leave "A one-click Launch Stack link will come later." as it is: the S3 bucket is still an owner step.

Repo docs:
8. `docs/SERVER.md` lines ~84–87: `v3.0.0` → `v3.3.0` (URL and sentence).
9. `deploy/systemd/README.md` lines ~8 and ~33: raw URLs `v3.0.0` → `v3.3.0`.
10. `deploy/aws/README.md`:
    - line ~667: "Install `agenticml-lite` 3.3.0 from PyPI …".
    - lines ~789–792: `hosted_check.py` from the `v3.3.0` tag (the file exists at that tag).
    - Do NOT touch the `agenticml-lite@bd0d499` 2.2 pin.
11. `deploy/private-network/README.md` lines ~56–59: the example becomes "for version 3.3.0:" with `TAG=v3.3.0`. There is no `v3.2.0` tag, so the current URL returns 404. Keep `agenticml-lite>=3.2` everywhere: the minimum is still right.
12. `docs/PROTOCOL.md` line ~74: the User-Agent example `agenticml-lite/3.3.0`.

Do not change any other mention of 3.0.0 that is history: release notes, the 3.0 plan, "3.0 database", "Upgrading to 3.0", the 2.x→3.0 conversion, `PUBLISHED_3_0`, and so on. Do not touch `docs/plans/packages/*` other than this file. Do not change `website/package-lock.json`.

Roadmap (`docs/plans/on-prem-roadmap.md`):
13. Releases table:
    - 3.1 and 3.2 Exit criteria: append "; released in 3.3.0 (Nov 20, 2026), not published on its own".
    - 3.3: append "; released Nov 20, 2026 as 3.3.0 (tag `v3.3.0`, [PyPI](https://pypi.org/project/agenticml-lite/3.3.0/), [GitHub release](https://github.com/agenticML/agenticml-lite/releases/tag/v3.3.0))", in the 3.0 row's style. Do not claim that the exit criteria themselves are met: 34.5 and 36.5 are still open.
14. Status rows:
    - 34: "owner steps 34.4 publish and 34.5 left" → "34.4 published in 3.3.0 (Nov 20, 2026); owner step 34.5 left".
    - 34.4: done (released in 3.3.0, Nov 20, 2026: no separate 3.2.0 tag or upload).
    - 36: "done in code (this PR; …)" → "done ([#88](…/pull/88), merged Nov 20, 2026; released as 3.3.0 on Nov 20, 2026; owner steps 36.4 Launch Stack bucket and 36.5 first deploy left)".
    - 36.4: "done ([#88](…), released Nov 20, 2026: tag `v3.3.0`, PyPI 3.3.0, `agenticml.yaml` on the release); owner: Launch Stack bucket (Backlog)".
    - 35, 35.1, 35.2: `in review` → `done ([#90](https://github.com/agenticML/agenticml-lite/pull/90), merged Nov 20, 2026; released in 3.3.0)`.
    - In the 3.3 section intro, "(merged into this branch from PR #86, which must merge first)" becomes "(merged from PR #86)".
15. Backlog:
    - Delete the "**3.2.0 release** (package 34.4, owner)" entry. It is done, by 3.3.0 instead of a 3.2.0 tag.
    - "First lab install on a private network": change "(package 34.5, owner; after the "3.2.0 release" step above)" to "(package 34.5, owner; 3.3.0 is on PyPI)".
    - Rename "**3.3.0 release and Launch Stack link** (package 36.4, owner)" to "**Launch Stack link** (package 36.4, owner)".
      - Drop "Do after the 3.2.0 release step. PR #88 is stacked…".
      - Drop fix step (1) and renumber (2)–(4) to (1)–(3).
      - Problem: keep only the S3 quick-create reason.
      - Done when: "the Launch Stack link opens the quick-create page in a clean account". Add "3.3.0 was released on Nov 20, 2026 (tag, PyPI, `agenticml.yaml` on the release)".
    - Leave every other Backlog entry unchanged, including the `pypi` required-reviewer one in "Release follow-ups", which is still open.
    - Add this sentence to the Backlog intro paragraph: "3.3.0 (with 3.1 and 3.2) was released on Nov 20, 2026."
16. Add this package as a row under "### Maintenance fixes", in that table's format. Plan link: `packages/release-3-3-0-docs.md`. Status `in review`; the supervisor flips it on merge.

## Test (tests/test_website_facts.py)

Add a constant `PUBLISHED_TAGS = ('v3.0.0', 'v3.3.0')`, with a comment saying it is the published release tags and must be updated after each release. Add `test_raw_urls_use_published_tags`:
- Scan the docs-site `.md`/`.mdx` pages and the repo docs: README.md, docs/SERVER.md, docs/HOSTED.md, docs/PROTOCOL.md, deploy/systemd/README.md, deploy/aws/README.md, deploy/private-network/README.md, deploy/aws-self-hosted/README.md.
- Every `raw.githubusercontent.com/agenticML/agenticml-lite/<ref>/` URL must use a ref in `PUBLISHED_TAGS`. A `TAG=vX.Y.Z` assignment must also name a published tag. Branch refs like `main` must not appear; report them as bad.
- Fail if no raw URL is found at all, so the test cannot pass vacuously.
- Also check `$BASE`-style URLs: in `deploy/private-network/README.md`, `BASE=https://raw.githubusercontent.com/agenticML/agenticml-lite/$TAG/deploy` uses `$TAG`. Treat `$TAG` as allowed only when the file's `TAG=` value is published.

Add `test_releases_page_names_the_package_version`:
- `releases.md` "Which version" table rows say `{version} from PyPI`, where version is read from `pyproject.toml` with tomllib (Python 3.10 has no tomllib, so parse `^version = "(.*)"` with a regex instead).
- The `## {version}` section exists and its status line starts with `**Status:** Released`.

Because of the second test, a version bump makes the suite fail until the Releases page is updated. Write a docstring that says that is intended: bump the version in the release PR together with the page.

## Edge cases and constraints

- Keep the facts-test guarded sentences: free-plan limits, `requires client version 2.2 or newer`, the min-client sentences.
- Do not add tag URLs for `v3.2.0` anywhere.
- Plain prose, no marketing tone. Keep the existing house style: short sentences, Nov 20, 2026 date format.
- No code changes outside `tests/test_website_facts.py`.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests/test_website_facts.py`, then the full `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `git grep -nE "Not yet released|once it is published|v3\.2\.0|installs 3\.0\.0|3\.0\.0 from PyPI"` on the docs returns nothing, except history.
- No dashboard change, so the UI suite is not needed.

## Done when

The docs say 3.3.0 is current and released, no URL points at a missing tag, the roadmap and Backlog record the release, the new tests pass, and the full suite passes.
