---
title: Editing these docs
description: How to change or add a page on this site.
sidebar:
  order: 9
---

This site is built with [Astro Starlight](https://starlight.astro.build). Pages are Markdown files in [`website/src/content/docs/`](https://github.com/agenticML/agenticml-lite/tree/main/website/src/content/docs). The folder a page sits in decides its sidebar section.

To change a page, use **Edit page** at the bottom of it, or edit the file and open a pull request. The site is rebuilt and published when the change merges to `main`.

## Preview locally

Requires Node.js 22 or newer.

```bash
cd website
npm install
npm run dev
```

Then open the URL it prints, usually [http://localhost:4321/agenticml-lite/](http://localhost:4321/agenticml-lite/).

## Adding a page

Create a `.md` file in `getting-started/`, `guides/`, or `reference/` with a front matter block:

```md
---
title: My page
description: One sentence for search results.
sidebar:
  order: 5
---
```

Links between pages must include the `/agenticml-lite/` prefix, for example `/agenticml-lite/guides/dashboard/`.
