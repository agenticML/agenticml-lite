/* Snapshot diffs. Store strings enter the DOM only as text. */
import { tokenize, langFor } from "./code.js";

/**
 * Mount immediately; loading and errors stay inside the section.
 * `get(url, signal)` and DOM helpers follow app.js. apiBase defaults to /api.
 * Call update(freshRuns) from polling and destroy() when leaving the page.
 */
export function createCodeDiff({
  runs,
  colors = {},
  signal,
  el,
  get,
  button,
  apiBase = "/api",
}) {
  let currentRuns = runs,
    base = runs[0]?.run_id || "",
    target = runs[1]?.run_id || base,
    request = null,
    generation = 0,
    disposed = false,
    requestPair = null,
    trailingRefresh = false,
    pickerSignature = null,
    unchangedOpen = false,
    view = "merged",
    lastData = null;
  try {
    const stored = localStorage.getItem("agenticml-code-diff-view");
    if (["merged", "split"].includes(stored)) view = stored;
  } catch {}
  const openPaths = new Map(),
    scrollPaths = new Map();
  let fileNodes = new Map(), unchangedNode = null, unchangedSignature = null;
  const node = el("section", {
    class: "code-diff-section stack",
    "data-testid": "code-diff",
    "aria-label": "Code diff",
  });
  const summary = el("p", {
    class: "code-diff-summary",
    "data-testid": "code-diff-summary",
    "aria-live": "polite",
  });
  const content = el("div", { class: "code-diff-files" });
  const errors = el("div", { class: "code-diff-errors", role: "alert" });
  const baseSelect = picker("Base", "code-diff-base", (value) => {
    base = value;
    refresh(true);
  });
  const targetSelect = picker("Compare", "code-diff-compare", (value) => {
    target = value;
    refresh(true);
  });
  const swap = button("Swap", () => {
    [base, target] = [target, base];
    syncPickers();
    refresh(true);
  }, "ghost", { "aria-label": "Swap base and compare runs" });
  swap.classList.add("code-diff-swap");
  const merged = button("Merged", () => setView("merged"), "ghost", {
    "data-testid": "code-diff-view-merged",
    title: "Merged diff: both runs in one column",
  });
  const split = button("Split", () => setView("split"), "ghost", {
    "data-testid": "code-diff-view-split",
    title: "Split diff: base left, compare right",
  });
  syncView();
  node.append(
    el("h2", { class: "eyebrow", text: "Code diff" }),
    el(
      "div",
      {
        class: "code-diff-controls",
      },
      baseSelect.label,
      targetSelect.label,
      swap,
      el("div", { class: "code-diff-view", role: "group", "aria-label": "Diff view" }, merged, split),
    ),
    summary,
    errors,
    content,
  );

  function syncView() {
    node.dataset.view = view;
    merged.setAttribute("aria-pressed", String(view === "merged"));
    split.setAttribute("aria-pressed", String(view === "split"));
  }

  function setView(next) {
    if (next === view) return;
    view = next;
    try {
      localStorage.setItem("agenticml-code-diff-view", view);
    } catch {}
    syncView();
    remember();
    scrollPaths.clear();
    fileNodes.clear();
    if (lastData) draw(lastData, false);
  }

  function picker(label, testid, change) {
    const select = el("select", {
      class: "code-diff-select",
      "data-testid": testid,
      "aria-label": label,
      onchange: () => change(select.value),
    });
    return {
      select,
      label: el(
        "label",
        { class: "code-diff-picker" },
        el("span", { class: "code-diff-label", text: label }),
        select,
      ),
    };
  }

  function syncPickers() {
    const next = JSON.stringify(currentRuns.map((run) => [run.run_id, run.name]));
    for (const [picker, value] of [[baseSelect, base], [targetSelect, target]]) {
      if (next !== pickerSignature)
        picker.select.replaceChildren(
          ...currentRuns.map((run) => el("option", {
            value: run.run_id,
            text: run.name || run.run_id,
          })),
        );
      picker.select.value = value;
      picker.select.title = currentRuns.find((run) => run.run_id === value)?.name || value;
      picker.select.disabled = !currentRuns.length;
      picker.select.style.borderInlineStartColor = colors[value] || "";
    }
    swap.disabled = !currentRuns.length;
    pickerSignature = next;
  }

  function remember() {
    for (const file of content.querySelectorAll(".code-diff-file")) {
      openPaths.set(file.dataset.path, file.open);
      const scroll = file.querySelector(".code-diff-scroll");
      if (file.open && scroll)
        scrollPaths.set(file.dataset.path, [scroll.scrollLeft, scroll.scrollTop]);
    }
    const unchanged = content.querySelector(".code-diff-unchanged");
    if (unchanged) unchangedOpen = unchanged.open;
  }

  function textCell(line, lang, className = "") {
    const text = el("td", { class: `code-diff-text ${className}`.trim() });
    for (const token of tokenize(line.text, lang))
      text.append(el("span", {
        class: `diff-token-${token.k}`,
        text: token.t,
      }));
    if (line.clipped) {
      text.title = "This line was shortened to fit the diff preview.";
      text.append(el("span", { class: "code-diff-clipped", text: "…" }));
    }
    return text;
  }

  function patch(file) {
    const body = el("tbody");
    const lang = langFor(file.path);
    for (const hunk of file.hunks) {
      body.append(el("tr", { class: "code-diff-hunk" }, el("td", {
        colSpan: 4,
        text: `@@ -${hunk.base_start},${hunk.base_count} +${hunk.target_start},${hunk.target_count} @@`,
      })));
      for (const line of hunk.lines) {
        const type = ["added", "removed"].includes(line.type) ? line.type : "context";
        body.append(el("tr", { class: `code-diff-line diff-${type}` },
          el("td", {
            class: "code-diff-base-line",
            text: line.base_line ?? "",
          }),
          el("td", {
            class: "code-diff-target-line",
            text: line.target_line ?? "",
          }),
          el("td", {
            class: "code-diff-marker",
            text: type === "added" ? "+" : type === "removed" ? "−" : " ",
          }),
          textCell(line, lang),
        ));
      }
    }
    return el("div", {
      class: "code-diff-scroll",
      tabIndex: 0,
      role: "region",
      "aria-label": `Diff for ${file.path}`,
    }, el("table", {
      class: "code-diff-table",
      "aria-label": "Base line, compare line, change, source",
    }, body));
  }

  function splitPatch(file) {
    const body = el("tbody");
    const lang = langFor(file.path);
    const side = (line, name, type) => {
      const change = line ? `diff-${type}` : "code-diff-filler";
      const classes = `code-diff-side-${name} ${change}`;
      return [
        el("td", {
          class: `code-diff-${name === "base" ? "base" : "target"}-line ${change}`,
          text: line?.[name === "base" ? "base_line" : "target_line"] ?? "",
        }),
        el("td", {
          class: `code-diff-marker ${classes}`,
          text: !line || type === "context" ? "" : type === "removed" ? "−" : "+",
        }),
        line ? textCell(line, lang, classes) : el("td", { class: `code-diff-text ${classes}` }),
      ];
    };
    const row = (baseLine, targetLine, baseType, targetType) => body.append(el("tr", {
      class: "code-diff-line code-diff-split-row",
      "data-base-type": baseLine ? baseType : "empty",
      "data-target-type": targetLine ? targetType : "empty",
    }, ...side(baseLine, "base", baseType), ...side(targetLine, "target", targetType)));
    for (const hunk of file.hunks) {
      body.append(el("tr", { class: "code-diff-hunk" }, el("td", {
        colSpan: 6,
        text: `@@ -${hunk.base_start},${hunk.base_count} +${hunk.target_start},${hunk.target_count} @@`,
      })));
      let index = 0;
      while (index < hunk.lines.length) {
        const line = hunk.lines[index];
        if (!["removed", "added"].includes(line.type)) {
          row(line, line, "context", "context");
          index++;
          continue;
        }
        const removed = [], added = [];
        while (hunk.lines[index]?.type === "removed") removed.push(hunk.lines[index++]);
        while (hunk.lines[index]?.type === "added") added.push(hunk.lines[index++]);
        for (let i = 0; i < Math.max(removed.length, added.length); i++)
          row(removed[i], added[i], "removed", "added");
      }
    }
    return el("div", {
      class: "code-diff-scroll",
      tabIndex: 0,
      role: "region",
      "aria-label": `Diff for ${file.path}`,
    }, el("table", {
      class: "code-diff-table code-diff-split",
      "aria-label": "Base line, base source, compare line, compare source",
    }, el("colgroup", {},
      ...[56, 20, null, 56, 20, null].map((width) => el("col", width ? { style: `width: ${width}px` } : {})),
    ), body));
  }

  function fileDetails(file, initiallyOpen) {
    const status = { added: "Added", modified: "Modified", removed: "Removed" }[file.status];
    const details = el("details", {
      class: "code-diff-file",
      "data-testid": "code-diff-file",
      "data-path": file.path,
      "data-status": file.status,
      open: openPaths.get(file.path) ?? initiallyOpen,
    }, el("summary", { class: "code-diff-file-summary" },
      el("span", {
        class: `code-diff-status diff-status-${file.status}`,
        "data-testid": "code-diff-status",
        text: status,
      }),
      document.createTextNode(" "),
      el("span", { class: "code-diff-path", text: file.path }),
      document.createTextNode(" "),
      el("span", {
        class: "code-diff-counts",
        text: file.binary ? "binary" : file.error ? "unavailable" : file.too_large ? "too large" :
          file.added == null || file.deleted == null ? "not diffed" : `+${file.added} −${file.deleted}`,
      }),
    ));
    let scroll = null;
    const mount = () => {
      if (!scroll && file.hunks.length) {
        scroll = view === "split" ? splitPatch(file) : patch(file);
        details.append(scroll);
        scroll.addEventListener("scroll", () => {
          if (details.open && content.contains(details))
            scrollPaths.set(file.path, [scroll.scrollLeft, scroll.scrollTop]);
        });
      }
      const position = scrollPaths.get(file.path);
      if (scroll && position) [scroll.scrollLeft, scroll.scrollTop] = position;
    };
    details.addEventListener("toggle", () => {
      // Removed nodes may still have a queued toggle event after a poll.
      if (!content.contains(details)) return;
      openPaths.set(file.path, details.open);
      if (details.open) mount();
    });
    for (const [show, text] of [
      [file.binary, "Binary file — not shown"],
      [file.too_large && !file.error, "Too large to diff"],
    ])
      if (show) details.append(el("p", { class: "code-diff-note", text }));
    for (const [show, text] of [
      [file.error, "Snapshot unavailable"],
      [file.patch_truncated, "Diff truncated"],
      [file.approximate, "Approximate diff (large file)"],
      [file.newline_change, "Newline at end of file changed"],
    ])
      if (show) details.querySelector("summary").append(el("span", { class: "code-diff-badge", text }));
    if (details.open) mount();
    return details;
  }

  function draw(data, rememberState = true) {
    lastData = data;
    if (rememberState) remember();
    // Replacement can briefly shrink the page and clamp ancestor scrolling.
    const ancestors = [];
    for (let parent = node.parentElement; parent; parent = parent.parentElement)
      ancestors.push([parent, parent.scrollLeft, parent.scrollTop]);
    const totals = data.summary;
    const changed = totals.added + totals.modified + totals.removed;
    const empty = !data.files.length && Object.values(totals).every((value) => value === 0);
    summary.textContent = empty ? "No code snapshots recorded for these runs." :
      `${changed} ${changed === 1 ? "file" : "files"} changed · +${totals.lines_added} −${totals.lines_deleted} · ${totals.unchanged} unchanged ${totals.unchanged === 1 ? "file" : "files"}`;
    const children = [];
    if (empty)
      children.push(el("p", {
        class: "code-diff-empty",
        text: "No code snapshots recorded for these runs.",
      }));
    else if (!changed)
      children.push(el("p", { class: "code-diff-empty", text: "Code is identical." }));
    let withHunks = 0;
    const nextFiles = new Map();
    for (const file of data.files.filter((file) => file.status !== "unchanged")) {
      const initiallyOpen = file.hunks.length > 0 && withHunks++ < 3;
      const signature = JSON.stringify([view, file]);
      const previous = fileNodes.get(file.path);
      const details = previous?.signature === signature ? previous.node : fileDetails(file, initiallyOpen);
      nextFiles.set(file.path, { signature, node: details });
      children.push(details);
    }
    fileNodes = nextFiles;
    const unchangedPaths = data.files.filter((file) => file.status === "unchanged").map((file) => file.path);
    const nextUnchangedSignature = JSON.stringify(unchangedPaths);
    if (unchangedPaths.length) {
      if (nextUnchangedSignature !== unchangedSignature) unchangedNode = el("details", {
        class: "code-diff-unchanged",
        open: unchangedOpen,
      }, el("summary", { text: `${unchangedPaths.length} unchanged ${unchangedPaths.length === 1 ? "file" : "files"}` }),
      el("ul", { class: "code-diff-unchanged-list" },
        ...unchangedPaths.map((path) => el("li", { class: "code-diff-path", text: path })),
      ));
      children.push(unchangedNode);
    }
    unchangedSignature = nextUnchangedSignature;
    if (data.truncated)
      children.push(el("p", { class: "code-diff-note", text:
        `Showing ${data.files.length} of ${data.files_total} files · ${data.files_total - data.files.length} omitted` }));
    // Keep unchanged nodes attached: replacing or moving them loses keyboard focus.
    const retained = new Set(children);
    for (const child of [...content.children]) if (!retained.has(child)) child.remove();
    children.forEach((child, index) => {
      if (content.children[index] !== child) content.insertBefore(child, content.children[index] || null);
    });
    for (const file of content.querySelectorAll(".code-diff-file")) {
      const scroll = file.querySelector(".code-diff-scroll");
      const position = scrollPaths.get(file.dataset.path);
      if (file.open && scroll && position) [scroll.scrollLeft, scroll.scrollTop] = position;
    }
    for (const [parent, left, top] of ancestors) {
      parent.scrollLeft = left;
      parent.scrollTop = top;
    }
  }

  async function refresh(reset = false) {
    if (disposed) return;
    const pair = JSON.stringify([base, target]);
    if (request && requestPair === pair) {
      trailingRefresh = true;
      return;
    }
    const token = ++generation;
    request?.abort();
    request = null;
    requestPair = pair;
    trailingRefresh = false;
    syncPickers();
    errors.replaceChildren();
    if (reset) {
      remember();
      lastData = null;
      fileNodes.clear();
      unchangedNode = null;
      unchangedSignature = null;
      summary.textContent = "";
      content.replaceChildren(el("p", {
        class: "code-diff-loading",
        text: base && target ? "Loading code diff…" : "Select two runs to compare code.",
      }));
    }
    if (!base || !target) return;
    const pending = new AbortController();
    request = pending;
    node.setAttribute("aria-busy", "true");
    try {
      const query = new URLSearchParams({ base, target });
      const data = await get(`${apiBase.replace(/\/$/, "")}/compare/code?${query}`, pending.signal);
      if (!disposed && token === generation) draw(data);
    } catch (error) {
      if (!disposed && token === generation && error.name !== "AbortError")
        errors.replaceChildren(el("p", {
          class: "code-diff-error error",
          text: error.message || "Unable to load code diff.",
        }));
    } finally {
      if (!disposed && token === generation) {
        node.setAttribute("aria-busy", "false");
        request = null;
        content.querySelector(".code-diff-loading")?.remove();
        if (trailingRefresh) {
          trailingRefresh = false;
          refresh();
        }
      }
    }
  }

  function destroy() {
    if (disposed) return;
    disposed = true;
    generation++;
    request?.abort();
    signal?.removeEventListener("abort", destroy);
  }

  signal?.addEventListener("abort", destroy, { once: true });
  if (signal?.aborted) destroy();
  else refresh(true);
  return {
    node,
    update(nextRuns) {
      if (disposed) return;
      // Include the terminal poll: the last snapshot may arrive with finish.
      const wasRunning = currentRuns.some((run) =>
        (run.run_id === base || run.run_id === target) && run.status === "running");
      currentRuns = nextRuns;
      const previous = [base, target];
      if (!currentRuns.some((run) => run.run_id === base))
        base = currentRuns[0]?.run_id || "";
      if (!currentRuns.some((run) => run.run_id === target))
        target = currentRuns[1]?.run_id || base;
      syncPickers();
      const changed = base !== previous[0] || target !== previous[1];
      if (changed || wasRunning || currentRuns.some((run) =>
        (run.run_id === base || run.run_id === target) && run.status === "running"))
        return refresh(changed);
    },
    destroy,
  };
}
