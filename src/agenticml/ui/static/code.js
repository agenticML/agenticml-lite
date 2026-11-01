/* Read-only code snapshots. Syntax and line diff follow the hosted viewer. */
const PY_KW = new Set(
  "def class return if else elif for in import from as with not and or None True False lambda yield try except raise pass while is async await".split(
    " ",
  ),
);
const PY_BI = new Set(
  "self super len range enumerate sum min max float int str dict list tuple set print sorted zip isinstance".split(
    " ",
  ),
);
const SH_KW = new Set(
  "set if then fi exit test echo export git torchrun".split(" "),
);

export const LANG_NAME = {
  py: "Python",
  yaml: "YAML",
  sh: "Shell",
  json: "JSON",
  text: "Text",
};

export function langFor(path) {
  const ext = path.split(".").pop()?.toLowerCase();
  if (ext === "py") return "py";
  if (ext === "yaml" || ext === "yml") return "yaml";
  if (ext === "sh" || ext === "bash") return "sh";
  if (ext === "json") return "json";
  return "text";
}

/** Line-at-a-time highlighter — no multi-line state beyond a docstring opener. */
export function tokenize(src, lang) {
  const toks = [];
  const push = (t, k) => {
    if (!t) return;
    const last = toks[toks.length - 1];
    if (last && last.k === k) last.t += t;
    else toks.push({ t, k });
  };
  if (lang === "text") return [{ t: src, k: "p" }];
  let s = src;
  if (lang === "yaml" || lang === "json") {
    const m = s.match(
      lang === "yaml"
        ? /^(\s*-?\s*)([\w./-]+)(:)(.*)$/
        : /^(\s*)("(?:[^"\\]|\\.)*")(:)(.*)$/,
    );
    if (m) {
      push(m[1], "p");
      push(m[2], "key");
      push(":", "p");
      s = m[4];
    }
  }
  if (lang === "py" && /^\s*("""|''')/.test(s)) {
    push(s, "str");
    return toks;
  }
  const re =
    /(#.*$)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(@[\w.]+)|(\$\(?\w*)|(--[a-zA-Z][\w-]*)|(\b\d[\d_]*\.?\d*(?:e-?\d+)?\b)|([A-Za-z_]\w*)|(\s+|.)/g;
  let m;
  let prev = "";
  while ((m = re.exec(s))) {
    if (m[1]) push(m[1], lang === "json" ? "p" : "com");
    else if (m[2]) push(m[2], "str");
    else if (m[3]) push(m[3], lang === "py" ? "dec" : "p");
    else if (m[4]) push(m[4], lang === "sh" ? "dec" : "p");
    else if (m[5]) push(m[5], lang === "sh" ? "bi" : "p");
    else if (m[6]) push(m[6], "num");
    else if (m[7]) {
      const w = m[7];
      let k = "p";
      if (lang === "py")
        k = PY_KW.has(w)
          ? "kw"
          : prev === "def" || prev === "class"
            ? "fn"
            : PY_BI.has(w)
              ? "bi"
              : "p";
      else if (lang === "yaml" || lang === "json")
        k = /^(true|false|null)$/.test(w) ? "kw" : "p";
      else if (lang === "sh") k = SH_KW.has(w) ? "kw" : "p";
      push(w, k);
    } else push(m[8], "p");
    if (!/^\s+$/.test(m[0])) prev = m[0];
  }
  return toks;
}

export function outlineFor(lines, lang) {
  const out = [];
  lines.forEach((ln, i) => {
    let m;
    if (lang === "py" && (m = ln.match(/^(\s*)(def|class)\s+(\w+)/)))
      out.push({
        line: i,
        kind: m[2] === "class" ? "C" : "ƒ",
        label: m[3],
        depth: m[1].length ? 1 : 0,
      });
    else if (lang === "yaml" && (m = ln.match(/^(\s{0,2})([\w_]+):/)))
      out.push({ line: i, kind: "·", label: m[2], depth: m[1].length ? 1 : 0 });
    else if (lang === "sh" && (m = ln.match(/^([A-Z_]+)=/)))
      out.push({ line: i, kind: "$", label: m[1], depth: 0 });
    else if (lang === "sh" && /^torchrun/.test(ln))
      out.push({ line: i, kind: "▸", label: "torchrun", depth: 0 });
  });
  return out;
}

/** 0-based line index in `current` → the baseline text it replaced ('' when the line is new). */
export function changedLines(baseline, current) {
  const changed = {};
  let cur = 0;
  let pendingDel = [];
  for (const row of computeLineDiff(baseline, current)) {
    const text = row.text.slice(2);
    if (row.type === "removed") pendingDel.push(text);
    else if (row.type === "added") {
      changed[cur++] = pendingDel.shift() ?? "";
    } else {
      pendingDel = [];
      cur++;
    }
  }
  return changed;
}

export function computeLineDiff(leftText, rightText) {
  const L = leftText.split("\n");
  const R = rightText.split("\n");
  // A megabyte of short lines must not allocate a quadratic matrix.
  if (L.length * R.length > 200_000) {
    let prefix = 0,
      suffix = 0;
    while (prefix < Math.min(L.length, R.length) && L[prefix] === R[prefix])
      prefix++;
    while (
      suffix < Math.min(L.length, R.length) - prefix &&
      L[L.length - 1 - suffix] === R[R.length - 1 - suffix]
    )
      suffix++;
    return [
      ...L.slice(0, prefix).map((text) => ({
        type: "context",
        text: `  ${text}`,
      })),
      ...L.slice(prefix, L.length - suffix).map((text) => ({
        type: "removed",
        text: `- ${text}`,
      })),
      ...R.slice(prefix, R.length - suffix).map((text) => ({
        type: "added",
        text: `+ ${text}`,
      })),
      ...L.slice(L.length - suffix).map((text) => ({
        type: "context",
        text: `  ${text}`,
      })),
    ];
  }
  const rows = Array.from({ length: L.length + 1 }, () =>
    Array(R.length + 1).fill(0),
  );

  for (let i = L.length - 1; i >= 0; i--) {
    for (let j = R.length - 1; j >= 0; j--) {
      if (L[i] === R[j]) {
        rows[i][j] = rows[i + 1][j + 1] + 1;
      } else {
        rows[i][j] = Math.max(rows[i + 1][j], rows[i][j + 1]);
      }
    }
  }

  const diff = [];
  let i = 0,
    j = 0;
  while (i < L.length && j < R.length) {
    if (L[i] === R[j]) {
      diff.push({ type: "context", text: `  ${L[i]}` });
      i++;
      j++;
    } else if (rows[i + 1][j] >= rows[i][j + 1]) {
      diff.push({ type: "removed", text: `- ${L[i]}` });
      i++;
    } else {
      diff.push({ type: "added", text: `+ ${R[j]}` });
      j++;
    }
  }
  while (i < L.length) {
    diff.push({ type: "removed", text: `- ${L[i++]}` });
  }
  while (j < R.length) {
    diff.push({ type: "added", text: `+ ${R[j++]}` });
  }
  return diff;
}

const MAX_PREVIEW = 1024 * 1024;
const MAX_FILES = 60;

/** Shared snapshot cache keeps inline and full-window tabs lazy. */
export async function createCodeViewer({
  run,
  runs,
  signal,
  el,
  get,
  button,
  focus,
  objectURL,
  parentId,
  onBaseline,
}) {
  const api = (id) => `/api/runs/${encodeURIComponent(id)}/code`;
  const allFiles = (await get(api(run.run_id), signal)).sort((a, b) =>
    a.path.localeCompare(b.path),
  );
  // The snapshot records which file was launched; the name guess is a fallback
  // for manifests written before that was captured.
  const entry =
    allFiles.find((file) => file.source === "entrypoint")?.path ||
    allFiles.find((file) => /(train|main|run)[^/]*\.py$/.test(file.path))?.path;
  const first =
    allFiles.find((file) => /config/i.test(file.path))?.path ||
    allFiles[0]?.path;
  const required = new Set([first, entry].filter(Boolean));
  const chosen = new Set(required);
  for (const file of allFiles) {
    if (chosen.size >= MAX_FILES) break;
    chosen.add(file.path);
  }
  const manifest = allFiles.filter((file) => chosen.has(file.path));
  let baseline = null,
    baseManifest = [];
  // The lineage parent is the baseline when it has a snapshot; otherwise the
  // nearest earlier run in the project stands in.
  const parent = runs.find(
    (candidate) => parentId && candidate.run_id === parentId && candidate.run_id !== run.run_id,
  );
  const candidates = [
    ...(parent ? [parent] : []),
    ...runs
      .filter(
        (candidate) =>
          candidate !== parent &&
          candidate.run_id !== run.run_id &&
          (candidate.project || "") === (run.project || "") &&
          new Date(candidate.created_at) < new Date(run.created_at),
      )
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at)),
  ];
  // Probe in bounded parallel batches, retaining nearest-earlier ordering.
  for (let start = 0; start < candidates.length && !baseline; start += 4) {
    const batch = candidates.slice(start, start + 4);
    const manifests = await Promise.all(
      batch.map((candidate) => get(api(candidate.run_id), signal)),
    );
    const index = manifests.findIndex((files) => files.length);
    if (index >= 0) {
      baseline = batch[index];
      baseManifest = manifests[index];
    }
  }
  if (signal.aborted) return null;
  onBaseline?.(baseline, entry || null);
  const baseByPath = new Map(baseManifest.map((file) => [file.path, file]));
  const files = new Map(
    manifest.map((file) => [
      file.path,
      {
        ...file,
        lang: langFor(file.path),
        modified:
          !!baseline && baseByPath.get(file.path)?.sha256 !== file.sha256,
      },
    ]),
  );
  const cache = new Map();
  let disposed = false,
    fullWindow = null;
  const views = new Set();
  const tooLarge = () =>
    new Error("Too large: code previews are limited to 1 MB.");
  async function text(file) {
    if (file.size > MAX_PREVIEW) throw tooLarge();
    const download = new AbortController();
    const abort = () => download.abort();
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    let reader;
    try {
      const response = await fetch(objectURL(file.sha256, "text/plain"), {
        signal: download.signal,
      });
      if (!response.ok)
        throw new Error(`Code snapshot unavailable (${response.status})`);
      if (Number(response.headers.get("Content-Length")) > MAX_PREVIEW) {
        download.abort();
        throw tooLarge();
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parts = [];
      let size = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_PREVIEW) {
          download.abort();
          await reader.cancel().catch(() => {});
          throw tooLarge();
        }
        parts.push(decoder.decode(value, { stream: true }));
      }
      parts.push(decoder.decode());
      return parts.join("");
    } finally {
      reader?.releaseLock();
      signal.removeEventListener("abort", abort);
    }
  }
  function load(path) {
    if (!cache.has(path)) {
      const promise = (async () => {
        const file = files.get(path);
        const current = await text(file);
        const base = baseByPath.get(path);
        let prior = null,
          note = "";
        // Baseline errors must never hide a successfully loaded current file.
        if (file.modified && base) {
          try {
            prior = await text(base);
          } catch (error) {
            if (error.name === "AbortError") throw error;
            note = `Diff skipped: baseline unavailable. ${error.message}`;
          }
        }
        return {
          lines: current.split("\n"),
          note,
          changed:
            file.modified && !note
              ? prior == null
                ? Object.fromEntries(current.split("\n").map((_, i) => [i, ""]))
                : changedLines(prior, current)
              : {},
        };
      })();
      cache.set(path, promise);
      promise.then(
        (result) => {
          if (result.note) cache.delete(path);
        },
        () => cache.delete(path),
      );
    }
    return cache.get(path);
  }
  function view(full = false, initial = null) {
    const node = el("div", {
      class: "code-view",
      "data-testid": full ? "code-full-view" : "code-inline-view",
    });
    if (!first) {
      node.append(
        el("p", {
          class: "empty",
          text: "No code snapshot was uploaded for this run.",
        }),
      );
      return { node, destroy() {} };
    }
    let tabs = [...new Set([first, entry, initial?.path].filter(Boolean))],
      current = initial?.path || first,
      query = "",
      hits = [],
      hit = 0,
      wrap = false,
      data = null,
      generation = 0;
    const explorer = el(
      "nav",
      { class: "code-explorer", "aria-label": "Code files" },
      el(
        "header",
        {},
        el("span", { class: "eyebrow", text: "Snapshot files" }),
        el("span", {
          class: "muted",
          text: `${run.git?.git_commit ? `@ ${run.git.git_commit.slice(0, 7)} · ` : ""}read-only`,
        }),
      ),
    );
    if (allFiles.length > manifest.length)
      explorer.append(
        el("p", {
          class: "code-file-limit muted",
          text: `Showing ${manifest.length} of ${allFiles.length} files`,
        }),
      );
    let directory = null;
    for (const file of files.values()) {
      const dir = file.path.split("/").slice(0, -1).join("/");
      if (dir && dir !== directory)
        explorer.append(el("div", { class: "code-dir", text: `▾ ${dir}/` }));
      directory = dir;
      explorer.append(
        el(
          "button",
          {
            type: "button",
            class: "code-file",
            "data-path": file.path,
            "aria-label": `Open ${file.path}`,
            onclick: () => open(file.path),
          },
          el("span", { class: "lang-badge", text: file.lang }),
          el("span", { class: "filename", text: file.path.split("/").at(-1) }),
          el("span", {
            class: "file-marker",
            "data-modified": file.modified,
            title: file.modified
              ? baseline?.run_id === parentId
                ? "Modified vs parent"
                : "Modified vs baseline"
              : file.path === entry
                ? "Entrypoint"
                : "",
            text: file.modified ? "M" : file.path === entry ? "entrypoint" : "",
          }),
        ),
      );
    }
    const tabBar = el("div", {
      class: "code-tabs",
      role: "tablist",
      "aria-label": "Open files",
    });
    const crumb = el("span", { class: "code-crumb" });
    const input = el("input", {
      class: "field code-find",
      placeholder: "Find in file…",
      "aria-label": "Find in file",
    });
    const count = el("span", { class: "find-count", "aria-live": "polite" });
    const lines = el("div", {
      class: "code-lines",
      tabIndex: 0,
      "aria-label": "Source code",
      title: full ? "" : "Click to open in full window",
      onclick: full
        ? () => {}
        : (event) => {
            const row = event.target.closest(".code-line");
            openFull({
              path: current,
              line: row ? Number(row.dataset.line) - 1 : visibleLine(),
            });
          },
    });
    const status = el("div", { class: "code-status" });
    const outline = el("aside", {
      class: "code-outline",
      "aria-label": "Code outline",
    });
    const wrapButton = button(
      "Wrap",
      () => {
        wrap = !wrap;
        wrapButton.setAttribute("aria-pressed", wrap);
        lines.classList.toggle("wrap", wrap);
      },
      "ghost",
      { "aria-pressed": false },
    );
    const tools = el(
      "div",
      { class: "code-tools" },
      crumb,
      input,
      count,
      button("↑", () => step(-1), "ghost", { "aria-label": "Previous match" }),
      button("↓", () => step(1), "ghost", { "aria-label": "Next match" }),
      wrapButton,
    );
    node.append(
      explorer,
      el("div", { class: "code-editor" }, tabBar, tools, lines, status),
    );
    if (full) node.append(outline);
    function visibleLine() {
      const top = lines.getBoundingClientRect().top;
      for (let index = 0; index < lines.children.length; index++)
        if (lines.children[index].getBoundingClientRect().bottom > top)
          return index;
      return 0;
    }
    function jump(index) {
      const row = lines.children[index];
      if (row)
        lines.scrollTop = Math.max(
          0,
          row.offsetTop -
            lines.firstElementChild.offsetTop -
            lines.clientHeight / 3,
        );
    }
    function search() {
      const q = query.toLowerCase();
      hits =
        data && q
          ? data.lines.flatMap((line, index) =>
              line.toLowerCase().includes(q) ? [index] : [],
            )
          : [];
      if (hits.length) hit = ((hit % hits.length) + hits.length) % hits.length;
      count.textContent = q
        ? hits.length
          ? `${hit + 1} of ${hits.length}`
          : "none"
        : "";
      const hitSet = new Set(hits);
      for (const [index, row] of [...lines.children].entries()) {
        row.classList.toggle("hit", hitSet.has(index));
        row.classList.toggle("current", hits[hit] === index);
      }
      if (hits.length) jump(hits[hit]);
    }
    function step(direction) {
      hit += direction;
      search();
    }
    input.oninput = () => {
      query = input.value;
      hit = 0;
      search();
    };
    input.onkeydown = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        step(event.shiftKey ? -1 : 1);
      }
    };
    function drawChrome() {
      for (const file of explorer.querySelectorAll("[data-path]"))
        file.setAttribute("aria-current", file.dataset.path === current);
      tabBar.replaceChildren(
        ...tabs.map((path) =>
          el(
            "div",
            { class: `code-tab ${path === current ? "active" : ""}` },
            el("button", {
              role: "tab",
              "aria-selected": path === current,
              text: path.split("/").at(-1),
              onclick: () => open(path),
            }),
            el("button", {
              text: "×",
              "aria-label": `Close ${path}`,
              onclick: () => {
                tabs = tabs.filter((tab) => tab !== path);
                if (!tabs.length) tabs = [first];
                if (path === current) open(tabs.at(-1));
                else drawChrome();
              },
            }),
          ),
        ),
      );
      crumb.textContent = current.split("/").join(" › ");
    }
    function drawSource() {
      const file = files.get(current);
      lines.replaceChildren(
        ...data.lines.map((line, index) =>
          el(
            "div",
            {
              class: `code-line ${Object.hasOwn(data.changed, index) ? "changed" : ""}`,
              "data-line": index + 1,
              title: Object.hasOwn(data.changed, index)
                ? data.changed[index]
                  ? `was ${data.changed[index]}`
                  : "new line"
                : "",
            },
            el("span", { class: "line-number", text: index + 1 }),
            el("span", { class: "line-gutter" }),
            el(
              "span",
              { class: "line-text" },
              ...tokenize(line, file.lang).map((token) =>
                el("span", {
                  text: token.t,
                  style:
                    token.k === "p"
                      ? ""
                      : `color:var(--syn-${token.k});${token.k === "com" ? "font-style:italic" : ""}`,
                }),
              ),
            ),
          ),
        ),
      );
      const changed = Object.keys(data.changed).length;
      status.replaceChildren(
        el("span", { text: LANG_NAME[file.lang] }),
        el("span", { text: `${data.lines.length} lines` }),
        el("span", { text: "UTF-8 · LF" }),
        el("span", {
          text:
            data.note ||
            (changed
              ? `${changed} lines differ from baseline`
              : full
                ? baseline
                  ? "Identical to baseline"
                  : "No baseline in project"
                : "Click code to open full window"),
        }),
      );
      if (full) {
        outline.replaceChildren(
          el("div", { class: "eyebrow", text: "Outline" }),
        );
        const items = outlineFor(data.lines, file.lang);
        if (!items.length)
          outline.append(
            el("p", { class: "muted", text: "Nothing to outline" }),
          );
        for (const item of items)
          outline.append(
            el("button", {
              class: "outline-item",
              text: `${item.kind} ${item.label}  ${item.line + 1}`,
              style: `padding-left:${14 + item.depth * 14}px`,
              onclick: () => jump(item.line),
            }),
          );
        const modifiedFiles = [...files.values()].filter(
          (file) => file.modified,
        );
        if (baseline && modifiedFiles.length) {
          outline.append(
            el("div", {
              class: "eyebrow",
              text: `Differs from ${baseline.name || baseline.run_id.split("-").at(-1)}`,
            }),
          );
          let entries = 0;
          const changes = Object.entries(data.changed);
          const total = modifiedFiles.length + changes.length;
          for (const modified of modifiedFiles) {
            if (entries >= 200) break;
            entries++;
            outline.append(
              el("button", {
                class: "change-item",
                text: `M ${modified.path}`,
                onclick: () => open(modified.path),
              }),
            );
            if (modified.path === current)
              for (const [index, was] of changes) {
                if (entries >= 200) break;
                entries++;
                outline.append(
                  el(
                    "button",
                    {
                      class: "change-item",
                      onclick: () => jump(Number(index)),
                    },
                    el("span", {
                      class: "muted",
                      text: `${current.split("/").at(-1)}:${Number(index) + 1}`,
                    }),
                    el("span", { text: data.lines[index].trim() }),
                    el("span", {
                      class: "muted",
                      text: was ? `was ${was.trim()}` : "new line",
                    }),
                  ),
                );
              }
          }
          if (total > entries)
            outline.append(
              el("p", {
                class: "code-more muted",
                text: `and ${total - entries} more`,
              }),
            );
        }
      }
      search();
    }
    async function open(path, line = null) {
      if (!tabs.includes(path)) tabs.push(path);
      current = path;
      hit = 0;
      data = null;
      const ticket = ++generation;
      drawChrome();
      lines.replaceChildren(
        el("p", { class: "empty", text: "Loading snapshot…" }),
      );
      try {
        const loaded = await load(path);
        if (disposed || signal.aborted || ticket !== generation) return;
        data = loaded;
        drawSource();
        if (!query) lines.scrollTop = 0;
        if (line != null)
          requestAnimationFrame(() => {
            if (!disposed && ticket === generation && node.isConnected)
              jump(line);
          });
      } catch (error) {
        if (error.name !== "AbortError" && ticket === generation && !disposed)
          lines.replaceChildren(
            el("p", { class: "error", role: "alert", text: error.message }),
          );
      }
    }
    open(current, initial?.line);
    const result = {
      node,
      position: () => ({
        path: current,
        line: hits.length ? hits[hit] : visibleLine(),
      }),
      destroy() {
        generation++;
        views.delete(result);
      },
    };
    views.add(result);
    return result;
  }
  function openFull(position) {
    if (fullWindow || disposed) return;
    const fullView = view(true, position || inline.position());
    fullWindow = focus(
      run.name || run.run_id,
      "Snapshot as launched · read-only",
      "code-full",
      () => {
        fullView.destroy();
        fullWindow = null;
      },
    );
    fullWindow.host.querySelector(".focus-label").textContent = "Code";
    fullWindow.host.querySelector(".exit button").textContent = "Close · Esc";
    fullWindow.body.append(fullView.node);
  }
  const inline = view();
  return {
    node: inline.node,
    baseline,
    openFull,
    destroy() {
      disposed = true;
      fullWindow?.close();
      for (const item of views) item.destroy();
    },
  };
}
