# Package 39: Blog at agenticml.xyz/blog

Status: plan ready (Nov 27, 2026).

## Goal

Add a blog to the docs site at `https://agenticml.xyz/blog/`, with one
first post. The owner asked on Nov 27, 2026: "now i want to add a blog on the
agenticml-lite", then "have a test blog written". It ships in the same PR as
package 38 (the move to `agenticml.xyz`), so the blog goes live on its final URL.

URLs:

| What | URL |
| --- | --- |
| Post list | `/blog/` (more pages at `/blog/2/` and so on, once there are more posts) |
| A post | `/blog/<slug>/`; the slug is the file name |
| Tags | `/blog/tags/<tag>/` |
| RSS | `/blog/rss.xml` |

## Part 39.1: blog section (Sonnet)

### Approach

Use the Starlight plugin [`starlight-blog`](https://github.com/HiDeoo/starlight-blog)
(MIT, by a Starlight maintainer). Pin it to **`0.30.2`** exactly (released
Oct 14, 2026; its peer dependency is `@astrojs/starlight >=0.42.0`, and the site
pins `0.42.6`). `0.31.0` came out yesterday (Nov 26), so don't use it yet; we
don't take packages that are less than two weeks old.

1. `website/package.json`: add `"starlight-blog": "0.30.2"` (exact, no `^`).
   Update `package-lock.json` with `npm install` under Node 22.
2. `website/astro.config.mjs`: add `starlightBlog({...})` to `plugins` (before
   the links validator) with:
   - `title: 'Blog'`
   - `authors: { team: { name: 'agenticML team', url: 'https://github.com/agenticML' } }`
   - `postCount` and `recentPostCount` left at their defaults
   - leave `prefix` at its default `blog`.
3. `website/src/content.config.ts`: extend the docs schema with the
   plugin's `blogSchema` as its README says
   (`docsSchema({ extend: (context) => blogSchema(context) })`), keeping whatever
   is there now.
4. Posts live in `website/src/content/docs/blog/`. Frontmatter: `title`,
   `date` (`YYYY-MM-DD`), `authors: team`, `tags`, `excerpt`.
5. Component overrides: the site already overrides `SocialIcons` and
   `PageFrame`. The plugin overrides some Starlight components of its own. If
   the build warns or errors about a clash, make both work (for example, have
   our override render the plugin's component), and report what you did.
   The Contact us button, the hosted-preview banner (`routeData.ts`) and the
   GitHub icon must still show on blog pages.
6. The plugin adds a "Blog" link to the header; keep it. Add a "Blog" line to
   the README's docs list: `Blog: <https://agenticml.xyz/blog/>`.
7. `website/scripts/overflow-check.mjs` already walks every built page; make
   sure blog pages (`/blog/`, the post, the tag page) are included and add a
   check that `/blog/rss.xml` exists in `dist/` and has one `<item>` per
   post.
8. Add a test to `tests/test_website_facts.py`: every file in
   `website/src/content/docs/blog/` has `title`, a valid `date` that is not
   in the future, `authors` and `excerpt` in its frontmatter.

### Constraints

- No third-party scripts, analytics or comment widgets on blog pages.
- Don't change the docs pages or the sidebar beyond what the plugin adds.
- Pages stay at the root base from package 38 (no `/agenticml-lite`).

### Edge cases

- With one post, the list must not show an empty "older posts" link.
- The RSS feed's links must use `https://agenticml.xyz` (from `site`).
- At 375px, nothing overflows horizontally in either theme. This includes
  the post list, the post header with author and date, and the tag list.

## Part 39.2: first post (Antigravity writes it, the supervisor checks it)

File `website/src/content/docs/blog/new-home.md`, dated 2026-11-27, tags
`announcements`. The title says that the docs and blog now live at agenticml.xyz.
Keep it short (250–400 words) and stick to facts, with no marketing fluff:

- The docs moved from `agenticml.github.io/agenticml-lite/` to
  `agenticml.xyz`. Old links redirect.
- What agenticml-lite is, in two sentences. Local-first experiment tracking:
  runs are plain files under `~/.agenticml`, with a self-hosted server and a
  hosted service in public preview at `app.agenticml.xyz`.
- What the blog will cover: release notes and how-tos.
- Where to go next: links to the quickstart, the on-prem server and the
  hosted service, and to GitHub Discussions for questions.

Every claim must match the current docs pages. Use internal links at the root
(`/getting-started/quickstart/`).

## Checks

- In `website/` with Node 22: `npm ci && npm run build && npm run check:overflow`.
  The link validator must pass.
- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `npm run preview`, then open `/blog/`, the post and `/blog/rss.xml`.

## Done when

- `/blog/`, the first post, the tag page and `/blog/rss.xml` build. The overflow
  check and the facts test cover them and pass.
- After the package 38 cutover, `https://agenticml.xyz/blog/` serves the post.
