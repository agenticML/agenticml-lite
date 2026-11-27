# Package 38: Docs site at agenticml.xyz

Status: plan ready (Nov 27, 2026).

## Goal

Serve the docs site at `https://agenticml.xyz/` instead of
`https://agenticml.github.io/agenticml-lite/`, so that the blog (package 39)
starts on its final URL and post links never move. The owner asked to add a
blog on Nov 27, 2026, and chose `agenticml.xyz` over `docs.agenticml.xyz`
and over serving the pages from the app at `app.agenticml.xyz/docs` (same day).
This replaces the Backlog entry "Docs custom domain" (decision D8).

## URLs after the move

| What | URL |
| --- | --- |
| Home | `https://agenticml.xyz/` |
| Docs pages | same paths as today without `/agenticml-lite`, e.g. `https://agenticml.xyz/getting-started/installation/` |
| Policies (`HOSTED_LEGAL_BASE_URL`) | `https://agenticml.xyz/legal/` (`terms/`, `acceptable-use/`, `privacy/`) |
| Blog (package 39) | `https://agenticml.xyz/blog/`, `/blog/<slug>/`, an RSS feed under `/blog/` |
| `www.agenticml.xyz` | redirects to `https://agenticml.xyz/` (GitHub Pages does this when `www` points at Pages) |
| Old links | `https://agenticml.github.io/agenticml-lite/<path>` redirects to `https://agenticml.xyz/<path>` (GitHub Pages does this once the custom domain is set) |

Docs pages stay at the root, not under `/docs/`: it keeps every path the same
apart from the prefix, and the blog gets its own `/blog/` section.

## Part 38.1: root-path build (Sonnet)

### Files to touch

- `website/astro.config.mjs`: `site: 'https://agenticml.xyz'`, remove
  `base`, update the comment at the top. `editLink.baseUrl` points at the
  `dev` branch; `main` is the working branch now, so change it to `.../edit/main/website/`.
- Every page in `website/src/content/docs/`: internal links that start with
  `/agenticml-lite/` lose the prefix (Markdown links, `href="..."` in MDX,
  frontmatter `link:`). Absolute `https://agenticml.github.io/agenticml-lite/...`
  links become `https://agenticml.xyz/...`. Do not touch
  `github.com/agenticML/agenticml-lite` or `raw.githubusercontent.com/agenticML/agenticml-lite`
  URLs, or `pip install agenticml-lite`. Only the docs prefix changes.
- `website/src/components/*.astro`, `website/src/routeData.ts`: check them for a hard-coded base and fix any you find.
- `website/scripts/overflow-check.mjs`: `BASE = ''`, `SITE_ORIGIN = 'https://agenticml.xyz'`;
  make sure the local static server and the page list still work with an
  empty base (the page paths start with `/`, the 404 page is `/404.html`).
- Absolute docs URLs elsewhere become `https://agenticml.xyz/...` (same path,
  no prefix): `README.md` (links, the "Docs site:" line, and the Docs badge
  text `docs-agenticml.github.io` → `docs-agenticml.xyz`), `pyproject.toml`
  (`Homepage`, `Documentation`), `src/agenticml/ui/server.py` `_DOCS_URL`,
  `.github/ISSUE_TEMPLATE/config.yml` and `bug_report.yml`, `SECURITY.md`,
  `CONTRIBUTING.md`, `docs/*.md` (not `docs/plans/`, which is history),
  `deploy/**/README.md`.
- `scripts/monitor_hosted.py` `IGNORED_HOSTS`: add `agenticml.xyz` and
  `www.agenticml.xyz`; keep `agenticml.github.io`, because older releases and
  outside pages still link there.
- Tests: `tests/test_ui.py` expectations for the `Docs:` line;
  `tests/test_monitor_hosted.py` if it needs the new hosts. Add a guard test
  to `tests/test_website_facts.py`: (a) no file in the repo outside
  `docs/plans/`, `scripts/monitor_hosted.py` and `tests/` contains
  `agenticml.github.io/agenticml-lite`; (b) no file under
  `website/src/` contains a link that starts with `/agenticml-lite/`
  (in a Markdown link, `href=`, `link:`). Use `git ls-files` or a walk that skips
  `node_modules`, `dist`, `.venv` and `.git`.

### Constraints

- No other content or design changes. The blog is package 39.
- Do not add a `website/public/CNAME`: the site deploys through
  `actions/deploy-pages`, where GitHub ignores that file; the custom domain is set
  in the repository's Pages settings (38.2).
- Do not change `docs.yml`.
- Do not edit `docs/plans/` apart from what the supervisor asks.

### Edge cases

- Links with anchors (`/agenticml-lite/server/keys/#rotate`) and links
  without a trailing slash: keep the rest of the link as it is.
- `starlight-links-validator` runs at build time and must still pass.
- The 404 page must still load its CSS at the root.

### Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/` (the `Docs:` line in `agenticml ui` changes).
- In `website/` with Node 22: `npm ci && npm run build && npm run check:overflow`.
- `grep -rn "agenticml.github.io/agenticml-lite" . --exclude-dir={node_modules,dist,.git,.venv}`
  shows only `docs/plans/`, `scripts/monitor_hosted.py` and tests.

## Part 38.2: cutover (owner)

The order matters. Dropping `base` changes every asset URL, so the new build and
the custom domain have to go live together.

1. In Namecheap DNS for `agenticml.xyz`, add these records. Do not change `app`, the Zoho `MX` records or the `TXT` records.
   - `A` `@` → `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
   - optional `AAAA` `@` → `2606:50c0:8000::153`, `2606:50c0:8001::153`, `2606:50c0:8002::153`, `2606:50c0:8003::153`
   - `CNAME` `www` → `agenticml.github.io.`
   - remove any Namecheap "URL Redirect" or parking record on `@` or `www`.
   - Optionally, verify the domain for the `agenticML` organization (Settings → Pages → Verified domains, a `TXT` record) so no one else can claim it on Pages.
2. Wait until `dig +short agenticml.xyz` shows the four GitHub addresses.
3. Merge the 38.1 PR to `main`. `docs.yml` deploys the root-path build.
4. Right away, in the repository settings under Pages → Custom domain, enter
   `agenticml.xyz` and save. Between steps 3 and 4 the github.io site loads without
   styles. This lasts minutes.
5. When the certificate exists, tick **Enforce HTTPS**.

## Part 38.3: hosted policy links (owner)

1. Set the repository variable `HOSTED_LEGAL_BASE_URL` to `https://agenticml.xyz/legal/`.
2. Run the hosted deploy and approve it.
3. Check that the sign-in page at `app.agenticml.xyz` links to the three policies at the new URL.

## Done when

- `https://agenticml.xyz/` serves the site with HTTPS enforced. `www` and
  the old `agenticml.github.io/agenticml-lite/<path>` URLs redirect to it
  (check with `curl -sI`).
- The build, link validator, overflow check, unit tests and UI tests pass. The guard
  test is in place.
- The hosted sign-in page's policy links point at `agenticml.xyz/legal/`.
- PyPI's project links change with the next release. Until then the
  github.io redirect covers them.
