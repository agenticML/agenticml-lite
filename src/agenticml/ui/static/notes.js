/* Local run notes. Markdown is always built as DOM, never parsed as HTML. */
const noteURL = (id) => `/api/runs/${encodeURIComponent(id)}/notes`;
async function noteResponse(response) {
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(
      data.error || `Notes unavailable (${response.status})`,
    );
    if (response.status === 409) error.conflict = data.note;
    throw error;
  }
  return data;
}
export async function fetchNote(runId, signal) {
  return noteResponse(await fetch(noteURL(runId), { signal }));
}
export async function saveNote(
  runId,
  text,
  revision,
  { keepalive = false } = {},
) {
  const body = JSON.stringify({ text, revision });
  return noteResponse(
    await fetch(noteURL(runId), {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-AgenticML-Request": "1",
      },
      body,
      keepalive: keepalive && new TextEncoder().encode(body).length < 60000,
    }),
  );
}

// Keep source-line matching identical for rendering, counting, and toggling.
const taskLine = /^([ \t]*(?:[-*+]|\d+\.)[ \t]+\[)([ xX])(\][ \t](.*))$/;
const fenceLine = /^\s*```/;
export function toggleTask(text, lineIndex, checked) {
  // Retain the original separators, including CRLF, when changing a single mark.
  const lines = text.split(/(\r\n|\r|\n)/);
  let fenced = false;
  for (let i = 0; i / 2 <= lineIndex && i < lines.length; i += 2) {
    if (fenceLine.test(lines[i])) fenced = !fenced;
    else if (i / 2 === lineIndex && !fenced)
      lines[i] = lines[i].replace(
        taskLine,
        (_, before, mark, after) => `${before}${checked ? "x" : " "}${after}`,
      );
  }
  return lines.join("");
}
export function taskCounts(text) {
  let done = 0,
    total = 0,
    fenced = false;
  for (const line of text.replace(/\r\n?/g, "\n").split("\n")) {
    if (fenceLine.test(line)) fenced = !fenced;
    else if (!fenced) {
      const task = line.match(taskLine);
      if (task) {
        total++;
        if (task[2].toLowerCase() === "x") done++;
      }
    }
  }
  return { done, total };
}

const safeURL = (url) => /^(https?:|mailto:)/i.test(url) || /^#\//.test(url);
function inline(text, host, el, depth = 0) {
  if (depth > 12 || text.length > 2000) {
    host.append(document.createTextNode(text));
    return;
  }
  const tokens =
    /(`[^`\n]+`)|(\[([^\]\n]+)\]\(([^\s)]*)\))|(https?:\/\/[^\s<>]+)|(\*\*([^\n]+?)\*\*)|(~~([^\n]+?)~~)|(\*(\S(?:[^*\n]*?\S)?)\*)|(_([^_\n]+)_(?![A-Za-z0-9_]))/g;
  let end = 0,
    match;
  const link = (label, url) => {
    if (!safeURL(url)) return document.createTextNode(label);
    const node = el("a", { href: url });
    if (!url.startsWith("#/")) {
      node.target = "_blank";
      node.rel = "noopener noreferrer";
    }
    node.textContent = label;
    return node;
  };
  while ((match = tokens.exec(text))) {
    // Check the left boundary in code: regex lookbehind breaks older Safari
    // at module parse time. Retry after this underscore so a rejected snake_case
    // match cannot swallow the start of a later, valid emphasis token.
    if (match[12] && /[A-Za-z0-9_]/.test(text[match.index - 1] || "")) {
      tokens.lastIndex = match.index + 1;
      continue;
    }
    host.append(document.createTextNode(text.slice(end, match.index)));
    if (match[1]) host.append(el("code", { text: match[1].slice(1, -1) }));
    else if (match[2]) {
      host.append(
        safeURL(match[4])
          ? link(match[3], match[4])
          : document.createTextNode(match[2]),
      );
    } else if (match[5]) {
      const url = match[5].replace(/[.,;!?)]+$/, "");
      // Bare links have no recursive label parsing.
      const node = el("a", {
        href: url,
        text: url,
        target: "_blank",
        rel: "noopener noreferrer",
      });
      host.append(node, document.createTextNode(match[5].slice(url.length)));
    } else {
      const tag = match[6] ? "strong" : match[8] ? "del" : "em";
      const value = match[7] || match[9] || match[11] || match[13];
      const node = el(tag);
      inline(value, node, el, depth + 1);
      host.append(node);
    }
    end = tokens.lastIndex;
  }
  host.append(document.createTextNode(text.slice(end)));
}

export function renderMarkdown(text, { el, onToggle, interactive = false }) {
  const root = el("div", { class: "note-markdown" });
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let paragraph = null,
    quote = null,
    lists = [];
  const reset = () => {
    paragraph = null;
    quote = null;
    lists = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (fenceLine.test(line)) {
      reset();
      const code = [];
      while (++i < lines.length && !fenceLine.test(lines[i]))
        code.push(lines[i]);
      root.append(el("pre", {}, el("code", { text: code.join("\n") })));
      continue;
    }
    if (!line.trim()) {
      reset();
      continue;
    }
    let match;
    if ((match = line.match(/^\s*(#{1,6})\s+(.*)$/))) {
      reset();
      const heading = el(`h${Math.min(4, match[1].length)}`);
      inline(match[2], heading, el);
      root.append(heading);
    } else if (/^\s*(---|\*\*\*)\s*$/.test(line)) {
      reset();
      root.append(el("hr"));
    } else if ((match = line.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/))) {
      paragraph = null;
      quote = null;
      const depth = Math.floor(match[1].replace(/\t/g, "  ").length / 2);
      const kind = /\d/.test(match[2]) ? "ol" : "ul";
      let tail = lists[lists.length - 1];
      while (tail && tail.depth > depth) {
        lists.pop();
        tail = lists[lists.length - 1];
      }
      if (tail && tail.depth === depth && tail.kind !== kind) {
        lists.pop();
        tail = lists[lists.length - 1];
      }
      if (!tail || tail.depth < depth) {
        const list = el(kind);
        if (kind === "ol") list.start = parseInt(match[2], 10);
        (tail && tail.item ? tail.item : root).append(list);
        tail = { depth, kind, list, item: null };
        lists.push(tail);
      }
      const current = tail;
      const item = el("li");
      const task = line.match(taskLine);
      if (task) {
        const checked = task[2].toLowerCase() === "x";
        item.className = `note-task${checked ? " is-done" : ""}`;
        const label = el("label");
        const lineIndex = i;
        const checkbox = el("input", {
          type: "checkbox",
          checked,
          disabled: !interactive,
          "data-line": lineIndex,
          ...(task[4].trim() ? {} : { "aria-label": "Task" }),
          onchange: (event) => onToggle?.(lineIndex, event.target.checked),
        });
        const copy = el("span");
        inline(task[4], copy, el);
        label.append(checkbox, copy);
        item.append(label);
      } else inline(match[3], item, el);
      current.list.append(item);
      current.item = item;
    } else if ((match = line.match(/^\s*> ?(.*)$/))) {
      paragraph = null;
      lists = [];
      if (!quote) {
        quote = el("blockquote");
        root.append(quote);
      } else quote.append(el("br"));
      inline(match[1], quote, el);
    } else {
      quote = null;
      lists = [];
      if (!paragraph) {
        paragraph = el("p");
        root.append(paragraph);
      } else paragraph.append(el("br"));
      inline(line, paragraph, el);
    }
  }
  return root;
}

const launchNoteTitle =
  "Set with --notes / init(notes=...). Saving here creates this run's notepad.";
function relativeTime(value) {
  if (!value) return "";
  const elapsed = Math.max(0, Date.now() - Date.parse(value));
  if (!Number.isFinite(elapsed)) return "";
  const minutes = Math.floor(elapsed / 60000);
  return minutes < 1
    ? "just now"
    : minutes < 60
      ? `${minutes}m ago`
      : minutes < 1440
        ? `${Math.floor(minutes / 60)}h ago`
        : `${Math.floor(minutes / 1440)}d ago`;
}

/* Selection changes use the browser's editing APIs and the same input path as typing. */
function replaceText(area, replacement, start, end, mode = "end") {
  area.focus();
  area.setSelectionRange(start, end);
  let inserted = false;
  try {
    inserted = document.execCommand("insertText", false, replacement);
  } catch {}
  if (!inserted) {
    area.setRangeText(replacement, start, end, mode);
    area.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (mode === "select")
    area.setSelectionRange(start, start + replacement.length);
}
function editSelection(area, kind) {
  const start = area.selectionStart,
    end = area.selectionEnd;
  const text = area.value;
  area.focus();
  if (["Bold", "Italic", "Code", "Link"].includes(kind)) {
    const selected = text.slice(start, end) || "text";
    const marker = { Bold: "**", Italic: "*", Code: "`", Link: "[" }[kind];
    const replacement =
      kind === "Link"
        ? `[${selected}](https://)`
        : `${marker}${selected}${marker}`;
    replaceText(area, replacement, start, end);
    area.setSelectionRange(
      start + marker.length,
      start + marker.length + selected.length,
    );
  } else {
    const from = start === 0 ? 0 : text.lastIndexOf("\n", start - 1) + 1;
    const to = text.indexOf(
      "\n",
      end > start && text[end - 1] === "\n" ? end - 1 : end,
    );
    const until = to < 0 ? text.length : to;
    const prefixes = {
      Checklist: "- [ ] ",
      Bullet: "- ",
      Numbered: "1. ",
      Heading: "## ",
      Quote: "> ",
    };
    const patterns = {
      Checklist: /^[-*+] \[[ xX]\] /,
      Bullet: /^[-*+] (?!\[[ xX]\] )/,
      Numbered: /^\d+\. /,
      Heading: /^#{1,6} /,
      Quote: /^> /,
    };
    const lines = text.slice(from, until).split("\n");
    const nonblank = lines.filter((line) => line.trim());
    const remove =
      nonblank.length > 0 &&
      nonblank.every((line) => patterns[kind].test(line.trimStart()));
    let number = 0;
    const replacement = lines
      .map((line) => {
        // A single empty line is still an insertion point for the toolbar.
        if (lines.length > 1 && !line.trim()) return line;
        const indent = line.match(/^\s*/)[0];
        let body = line.slice(indent.length);
        if (remove) body = body.replace(patterns[kind], "");
        else {
          body = body.replace(
            /^(?:[-*+] (?:\[[ xX]\] )?|\d+\. |#{1,6} |> )/,
            "",
          );
          body =
            (kind === "Numbered" ? `${++number}. ` : prefixes[kind]) + body;
        }
        return indent + body;
      })
      .join("\n");
    replaceText(area, replacement, from, until, "select");
    if (start === end)
      area.setSelectionRange(
        from + replacement.length,
        from + replacement.length,
      );
  }
}
function continueList(event, area) {
  if (
    event.shiftKey ||
    event.isComposing ||
    area.selectionStart !== area.selectionEnd
  )
    return;
  const cursor = area.selectionStart;
  const from = cursor === 0 ? 0 : area.value.lastIndexOf("\n", cursor - 1) + 1;
  const before = area.value.slice(from, cursor);
  const match = before.match(/^(\s*)([-*+] (?:\[[ xX]\] )?|\d+\. )(.*)$/);
  if (!match) return;
  event.preventDefault();
  const nextLine = area.value.indexOf("\n", cursor);
  const until = nextLine < 0 ? area.value.length : nextLine;
  const rest = area.value.slice(cursor, until);
  if (!(match[3] + rest).trim()) replaceText(area, "", from, until);
  else {
    const number = match[2].match(/^(\d+)\. /);
    const marker = number
      ? `${Number(number[1]) + 1}. `
      : /\[[ xX]\]/.test(match[2])
        ? "- [ ] "
        : match[2];
    replaceText(area, `\n${match[1]}${marker}`, cursor, cursor);
  }
}

export function createNotepad({ runId, el, button, signal }) {
  let text = "",
    saved = "",
    revision = null,
    source = "none",
    writable = false,
    loaded = false;
  let editing = false,
    disposed = false,
    timer,
    pending = null,
    conflict = null;
  let failed = "",
    updatedAt = null,
    unloading = false,
    shiftEnter = false,
    unloadAttached = false,
    tooLarge = false,
    truncateArmed = false;
  const node = el("section", { class: "notepad", "aria-label": "Run notes" });
  const progress = el("span", { class: "chip note-progress", hidden: true });
  const saveState = el("span", {
    role: "status",
    "aria-live": "polite",
  });
  const saveTime = el("span", { class: "note-save-time" });
  const status = el("span", { class: "note-save muted" }, saveState, saveTime);
  const retry = button(
    "Retry",
    () => {
      // A closed gate stays closed. Retry must not arm a truncated write.
      if (gateClosed()) {
        showSaveAnyway();
        return;
      }
      failed = "";
      flush(false);
    },
    "ghost",
    { hidden: true },
  );
  const view = button("View", () => setMode(false), "ghost", {
    "aria-pressed": true,
  });
  const edit = button("Edit", () => setMode(true), "ghost", {
    "aria-pressed": false,
  });
  const modes = el("div", { class: "note-modes", hidden: true }, view, edit);
  const launchLabel = el("p", {
    class: "note-launch muted",
    text: "From launch notes",
    title: launchNoteTitle,
    hidden: true,
  });
  const banner = el("div", {
    class: "note-conflict",
    role: "alert",
    hidden: true,
  });
  const truncateCopy = el("p", {
    class: "note-truncate-copy",
    text: "This note is larger than 256 KiB and is shown truncated.",
  });
  const saveHint = el("p", {
    class: "note-truncate-hint",
    text: "Use Save anyway to replace the file.",
    hidden: true,
  });
  const saveAnyway = button("Save anyway", () => confirmTruncate());
  const truncateBanner = el(
    "div",
    { class: "note-truncate", role: "alert", hidden: true },
    truncateCopy,
    saveHint,
    saveAnyway,
  );
  // Gate closed: the file stays truncated until Save anyway arms one write.
  function gateClosed() {
    return tooLarge && !truncateArmed;
  }
  function showSaveAnyway() {
    if (!writable) return;
    saveHint.hidden = false;
    saveAnyway.focus();
  }
  function syncTruncate() {
    truncateCopy.textContent = writable
      ? "This note is larger than 256 KiB and is shown truncated. Saving will drop the rest of the file."
      : "This note is larger than 256 KiB and is shown truncated.";
    saveAnyway.hidden = !writable;
    if (!writable) saveHint.hidden = true;
    // A Ctrl+S hint belongs only to a closed gate. Once the gate opens, drop
    // it so a later conflict that sets tooLarge again does not show it.
    if (!gateClosed()) saveHint.hidden = true;
    truncateBanner.hidden = !tooLarge;
    const locked = !writable || !!conflict || gateClosed();
    for (const box of body.querySelectorAll('input[type="checkbox"]'))
      box.disabled = locked;
  }
  const body = el(
    "div",
    { class: "note-body" },
    el("p", { class: "muted", text: "Loading note…" }),
  );
  const area = el("textarea", {
    class: "note-editor",
    "aria-label": "Note Markdown",
    spellcheck: true,
    placeholder: "Write a note…",
    rows: 5,
  });
  const toolbar = el(
    "div",
    { class: "note-toolbar", role: "toolbar", "aria-label": "Note formatting" },
    ...[
      "Checklist",
      "Bullet",
      "Numbered",
      "Heading",
      "Bold",
      "Italic",
      "Code",
      "Quote",
      "Link",
    ].map((kind) =>
      button(kind, () => editSelection(area, kind), "ghost", {
        "aria-label": kind,
        title: kind,
        onmousedown: (event) => event.preventDefault(),
      }),
    ),
  );
  const editor = el("div", { class: "note-edit" }, toolbar, area);
  node.append(
    el("div", { class: "eyebrow", text: "Notes" }),
    el(
      "div",
      { class: "note-card" },
      el(
        "header",
        { class: "note-header" },
        el(
          "div",
          { class: "note-title" },
          el("h2", { text: "Notes" }),
          progress,
        ),
        el("div", { class: "note-controls" }, status, retry, modes),
      ),
      launchLabel,
      banner,
      truncateBanner,
      body,
    ),
  );
  const dirty = () => text !== saved;
  const unload = (event) => {
    const bytes = new TextEncoder().encode(
      JSON.stringify({ text, revision }),
    ).length;
    if (
      dirty() &&
      (conflict ||
        tooLarge ||
        failed ||
        !loaded ||
        !writable ||
        pending ||
        bytes >= 60000)
    ) {
      event.preventDefault();
      event.returnValue = "";
    }
    flush(true);
  };
  const pagehide = () => flush(true);
  const visibility = () => {
    if (document.visibilityState === "hidden") flush(true);
    else unloading = false;
  };
  window.addEventListener("pagehide", pagehide);
  document.addEventListener("visibilitychange", visibility);
  function autosave() {
    clearTimeout(timer);
    if (!conflict && !tooLarge) timer = setTimeout(() => flush(false), 800);
    paintStatus();
  }
  function paintStatus() {
    launchLabel.hidden = source !== "launch";
    const counts = taskCounts(text);
    progress.hidden = !counts.total;
    progress.textContent = `${counts.done}/${counts.total} done`;
    const state = !loaded
      ? ""
      : !writable
        ? "Read-only dashboard"
        : conflict
          ? "Unsaved"
          : failed
            ? "Save failed"
              : pending
              ? "Saving…"
              : tooLarge || dirty()
                ? "Unsaved"
                : updatedAt
                  ? "Saved"
                  : "Not saved yet";
    // Only state transitions mutate the live region. Relative ages are quiet.
    if (saveState.textContent !== state) saveState.textContent = state;
    const time = state === "Saved" ? ` · ${relativeTime(updatedAt)}` : "";
    if (saveTime.textContent !== time) saveTime.textContent = time;
    status.title = failed;
    retry.hidden = !failed;
    modes.hidden = !loaded || !writable;
    const needsUnload = dirty() && !disposed;
    if (needsUnload !== unloadAttached) {
      if (needsUnload) window.addEventListener("beforeunload", unload);
      else window.removeEventListener("beforeunload", unload);
      unloadAttached = needsUnload;
    }
  }
  function grow() {
    if (!editing || !area.isConnected) return;
    const { scrollX, scrollY } = window;
    area.style.height = "auto";
    area.style.height = `${Math.max(180, area.scrollHeight)}px`;
    window.scrollTo(scrollX, scrollY);
  }
  window.addEventListener("resize", grow);
  const relativeTimer = setInterval(paintStatus, 30000);
  function paintBody() {
    view.setAttribute("aria-pressed", !editing);
    edit.setAttribute("aria-pressed", editing);
    if (editing) {
      area.value = text;
      body.replaceChildren(editor);
      grow();
    } else if (!text.trim()) {
      body.replaceChildren(
        writable
          ? button("Add a note…", () => setMode(true), "ghost", {
              class: "note-add",
            })
          : el("p", { class: "muted", text: "No notes." }),
      );
    } else
      body.replaceChildren(
        renderMarkdown(text, {
          el,
          interactive: writable && !conflict && !gateClosed(),
          onToggle: (line, checked) => {
            text = toggleTask(text, line, checked);
            paintBody();
            paintStatus();
            flush(false);
          },
        }),
      );
  }
  function setMode(value) {
    if (!writable) return;
    editing = value;
    paintBody();
    if (editing) area.focus();
    else flush(false);
  }
  function showConflict() {
    banner.hidden = !conflict;
    banner.replaceChildren();
    if (!conflict) return;
    banner.append(
      el("p", { text: "This note was changed elsewhere." }),
      button("Load latest", () => {
        text = saved = conflict.text;
        revision = conflict.revision;
        updatedAt = conflict.updated_at;
        source = typeof conflict.source === "string" ? conflict.source : source;
        tooLarge = !!conflict.too_large;
        truncateArmed = false;
        conflict = null;
        failed = "";
        showConflict();
        syncTruncate();
        paintBody();
        paintStatus();
      }),
      button("Keep mine", () => {
        revision = conflict.revision;
        saved = conflict.text;
        source = typeof conflict.source === "string" ? conflict.source : source;
        tooLarge = !!conflict.too_large;
        truncateArmed = false;
        conflict = null;
        failed = "";
        showConflict();
        syncTruncate();
        paintBody();
        // A truncated file is replaced only from Save anyway.
        if (!tooLarge) flush(false);
        paintStatus();
      }),
    );
  }
  area.oninput = () => {
    text = area.value;
    failed = "";
    grow();
    autosave();
  };
  area.onblur = () => flush(false);
  area.onkeydown = (event) => {
    shiftEnter = event.key === "Enter" && event.shiftKey;
    if (
      (event.ctrlKey || event.metaKey) &&
      ["b", "i", "s"].includes(event.key.toLowerCase())
    ) {
      event.preventDefault();
      const key = event.key.toLowerCase();
      if (key === "s") {
        if (gateClosed()) showSaveAnyway();
        else flush(false);
      } else editSelection(area, key === "b" ? "Bold" : "Italic");
    } else if (event.key === "Enter") continueList(event, area);
  };
  // A canceled keydown suppresses beforeinput in browsers. IMEs that omit Enter
  // still supply beforeinput, so both paths share the same continuation logic.
  area.onbeforeinput = (event) => {
    if (["insertLineBreak", "insertParagraph"].includes(event.inputType)) {
      if (!shiftEnter) continueList(event, area);
      shiftEnter = false;
    }
  };
  function confirmTruncate() {
    if (!writable || !loaded) return;
    // An in-flight flush returns that promise and would leave a new arm set.
    if (pending) return;
    failed = "";
    if (conflict) {
      revision = conflict.revision;
      conflict = null;
      showConflict();
      paintBody();
    }
    truncateArmed = true;
    flush(false);
  }
  function flush(keepalive = false) {
    unloading = keepalive;
    clearTimeout(timer);
    // Saving replaces the whole file, so a truncated note stays local until
    // Save anyway arms one write. Autosave, ticks, unload, and Retry do not.
    const forced = truncateArmed && tooLarge;
    if (
      !writable ||
      !loaded ||
      conflict ||
      (tooLarge && !forced) ||
      (!dirty() && !forced)
    )
      return pending || Promise.resolve();
    if (pending) return pending;
    truncateArmed = false;
    const snapshot = text;
    pending = saveNote(runId, snapshot, revision, { keepalive })
      .then((note) => {
        revision = note.revision;
        updatedAt = note.updated_at;
        saved = note.text;
        if (typeof note.source === "string") source = note.source;
        if (text === snapshot) text = saved;
        failed = "";
        tooLarge = !!note.too_large;
        syncTruncate();
        document.dispatchEvent(new Event("agenticml-notes-saved"));
      })
      .catch((error) => {
        truncateArmed = false;
        if (error.conflict) {
          conflict = error.conflict;
          tooLarge = !!conflict.too_large;
          showConflict();
          syncTruncate();
          if (!editing) paintBody();
        } else failed = error.message;
      })
      .finally(() => {
        pending = null;
        paintStatus();
        // Changes made during this request use its returned revision, even after cleanup.
        if (!conflict && !failed && !tooLarge && dirty()) return flush(unloading);
      });
    paintStatus();
    return pending;
  }
  fetchNote(runId, signal)
    .then((note) => {
      if (disposed || signal?.aborted) return;
      text = saved = note.text;
      revision = note.revision;
      source = typeof note.source === "string" ? note.source : "none";
      writable = note.writable;
      updatedAt = note.updated_at;
      tooLarge = !!note.too_large;
      loaded = true;
      syncTruncate();
      paintBody();
      paintStatus();
    })
    .catch((error) => {
      if (disposed || error.name === "AbortError") return;
      body.replaceChildren(
        el("p", {
          class: "error",
          text: `Could not load notes. ${error.message}`,
        }),
        button("Reload notes", () => location.reload()),
      );
    });
  return {
    node,
    flush,
    destroy() {
      disposed = true;
      clearTimeout(timer);
      clearInterval(relativeTimer);
      if (unloadAttached) window.removeEventListener("beforeunload", unload);
      unloadAttached = false;
      window.removeEventListener("pagehide", pagehide);
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("resize", grow);
    },
  };
}

export function createNotebook({ el, button, get, routeRun }) {
  const node = el("details", { class: "notebook" });
  const summary = el("summary", { text: "Notes · loading…" });
  const body = el("div", { class: "notebook-body" });
  // Keep the live region connected while entries are redrawn.
  const announcement = el("span", {
    class: "note-live",
    role: "status",
    "aria-live": "polite",
  });
  node.append(summary, body, announcement);
  let notes = [],
    writable = false,
    truncated = false,
    project = null,
    query = "";
  let disposed = false,
    generation = 0,
    initialized = false,
    remembered = null;
  let pending = null,
    writing = 0,
    drawnKey = null,
    userToggle = false;
  const messages = new Map(),
    messageTimers = new Map(),
    messageNodes = new Map(),
    expanded = new Set(),
    saving = new Set();
  const abort = new AbortController();
  try {
    remembered = localStorage.getItem("agenticml-notebook-open");
  } catch {}
  summary.addEventListener("click", (event) => {
    if (event.isTrusted) userToggle = true;
  });
  node.ontoggle = () => {
    if (userToggle) {
      userToggle = false;
      remembered = String(node.open);
      try {
        localStorage.setItem("agenticml-notebook-open", remembered);
      } catch {}
    }
    if (node.open) measureMore();
  };
  function measureMore() {
    requestAnimationFrame(() => {
      if (disposed || !node.open) return;
      for (const entry of body.querySelectorAll(".notebook-entry")) {
        const content = entry.querySelector(".notebook-content");
        const bottom = content.getBoundingClientRect().bottom;
        // Keep visible tasks usable, but exclude clipped controls (including
        // links) from focus and the accessibility tree until expanded.
        for (const control of content.querySelectorAll("input, a")) {
          const clipped = !expanded.has(entry.dataset.runId) &&
            control.getBoundingClientRect().bottom > bottom;
          control.toggleAttribute("inert", clipped);
        }
        entry.querySelector(".note-more").hidden =
          !expanded.has(entry.dataset.runId) &&
          content.scrollHeight <= content.clientHeight + 1;
      }
    });
  }
  function showMessage(runId, message) {
    clearTimeout(messageTimers.get(runId));
    messages.set(runId, message);
    announcement.textContent = message;
    messageTimers.set(
      runId,
      setTimeout(() => {
        messages.delete(runId);
        messageNodes.get(runId)?.remove();
        messageNodes.delete(runId);
        messageTimers.delete(runId);
        if (announcement.textContent === message) announcement.textContent = "";
      }, 5000),
    );
  }
  function updateTimes() {
    const byId = new Map(notes.map((note) => [note.run_id, note]));
    for (const entry of body.querySelectorAll(".notebook-entry")) {
      const time = relativeTime(byId.get(entry.dataset.runId)?.updated_at);
      entry.querySelector(".note-time").textContent = time ? `edited ${time}` : "";
    }
  }
  function draw(force = false) {
    if (!initialized) return;
    const key = JSON.stringify([
      notes.map(({ run_id, revision, name, project, status, updated_at, too_large, source: noteSource }) => [
        run_id, revision, name, project, status, updated_at, !!too_large, noteSource,
      ]),
      writable, truncated, project, query, [...expanded],
    ]);
    if (!force && key === drawnKey) return;
    drawnKey = key;
    const active = document.activeElement;
    const focusedEntry = active?.closest(".notebook-entry");
    const focusedRun = focusedEntry?.dataset.runId;
    const focusSelector = active?.matches("input[data-line]")
      ? `input[data-line="${active.dataset.line}"]`
      : active?.matches(".note-more") ? ".note-more" : ".notebook-run";
    const shown = notes.filter(
      (note) =>
        (project === null || note.project === project) &&
        `${note.name} ${note.run_id} ${note.text}`
          .toLowerCase()
          .includes(query.toLowerCase()),
    );
    const open = shown.reduce((sum, note) => {
      const counts = taskCounts(note.text);
      return sum + counts.total - counts.done;
    }, 0);
    summary.replaceChildren(
      el("span", { text: `Notes · ${shown.length} run${shown.length === 1 ? "" : "s"}` }),
      el("span", {
        class: "muted",
        text: `${open} open task${open === 1 ? "" : "s"}`,
      }),
    );
    body.replaceChildren();
    messageNodes.clear();
    for (const note of shown) {
      const counts = taskCounts(note.text);
      const status = note.status || "unknown";
      const entry = el("article", {
        class: "notebook-entry",
        "data-run-id": note.run_id,
      });
      const content = el("div", {
        class: `notebook-content${expanded.has(note.run_id) ? " is-expanded" : ""}`,
      });
      const header = el(
        "header",
        { class: "notebook-entry-header" },
        el("a", {
          class: "mono notebook-run",
          href: routeRun(note.run_id),
          text: note.name || note.run_id,
          title: note.name || note.run_id,
        }),
        note.source === "launch"
          ? el("span", {
              class: "note-launch muted",
              text: "From launch notes",
              title: launchNoteTitle,
            })
          : null,
        el("span", {
          class: `chip ${{ running: "accent", finished: "ok", crashed: "danger", stale: "warn" }[status] || ""}`,
          text: status,
        }),
        project === null
          ? el("span", { class: "muted", text: note.project || "No project" })
          : null,
        el("span", {
          class: "muted note-time",
          text: note.updated_at ? `edited ${relativeTime(note.updated_at)}` : "",
        }),
        counts.total
          ? el("span", {
              class: "chip",
              text: `${counts.done}/${counts.total}`,
              title: "Tasks completed",
            })
          : null,
      );
      content.append(
        renderMarkdown(note.text, {
          el,
          interactive: writable && !note.too_large && !saving.has(note.run_id),
          onToggle: async (line, checked) => {
            if (note.too_large || saving.has(note.run_id)) return;
            saving.add(note.run_id);
            writing++;
            generation++;
            const text = toggleTask(note.text, line, checked);
            messages.delete(note.run_id);
            // Disable only this entry until its compare-and-swap completes.
            for (const box of entry.querySelectorAll('input[type="checkbox"]'))
              box.disabled = true;
            try {
              const result = await saveNote(note.run_id, text, note.revision);
              Object.assign(note, result);
            } catch (error) {
              showMessage(
                note.run_id,
                error.conflict
                  ? "This note was changed elsewhere. Loaded the latest version."
                  : `Save failed. ${error.message}`,
              );
              if (error.conflict) Object.assign(note, error.conflict);
            } finally {
              saving.delete(note.run_id);
              writing--;
              if (!disposed) {
                draw(true);
                refresh();
              }
            }
          },
        }),
      );
      entry.append(header);
      if (note.too_large)
        entry.append(
          el("p", {
            class: "note-truncated",
            text: "Truncated: open the run to edit",
          }),
        );
      entry.append(content);
      const more = button(
        expanded.has(note.run_id) ? "Show less" : "Show more",
        () => {
          if (expanded.has(note.run_id)) expanded.delete(note.run_id);
          else expanded.add(note.run_id);
          draw();
        },
        "ghost",
        {
          class: "btn btn-ghost btn-sm note-more",
          "aria-expanded": expanded.has(note.run_id),
          hidden: true,
        },
      );
      entry.append(more);
      if (messages.has(note.run_id)) {
        const message = el("p", {
          class: "note-message",
          text: messages.get(note.run_id),
        });
        messageNodes.set(note.run_id, message);
        entry.append(message);
      }
      body.append(entry);
      if (focusedRun === note.run_id) {
        // A hidden Show-more control cannot receive focus before the next frame.
        if (focusSelector === ".note-more")
          more.hidden = !expanded.has(note.run_id) &&
            content.scrollHeight <= content.clientHeight + 1;
        const target = entry.querySelector(focusSelector);
        (target?.hidden ? entry.querySelector(".notebook-run") : target)
          ?.focus({ preventScroll: true });
      }
    }
    if (!shown.length)
      body.append(
        el("p", {
          class: "muted",
          text: notes.length
            ? "No notes match this filter."
            : "No notes yet. Open a run to add one.",
        }),
      );
    if (truncated)
      body.append(
        el("p", {
          class: "muted",
          text: "Some notes are hidden. The notebook size limit was reached.",
        }),
      );
    measureMore();
  }
  async function refresh() {
    if (disposed || writing) return;
    if (pending) return pending;
    const token = ++generation;
    pending = get("/api/notes", abort.signal)
      .then((data) => {
        if (disposed || token !== generation) return;
        // Retain objects captured by entry handlers when an unchanged poll skips draw.
        const existing = new Map(notes.map((note) => [note.run_id, note]));
        notes = data.notes.map((note) => {
          const current = existing.get(note.run_id) || {};
          if (!note.too_large) delete current.too_large;
          return Object.assign(current, note);
        });
        writable = data.writable;
        truncated = data.truncated;
        if (!initialized) {
          node.open =
            remembered === null ? !!notes.length : remembered === "true";
          initialized = true;
        }
        draw();
      })
      .catch((error) => {
        if (disposed || error.name === "AbortError" || token !== generation)
          return;
        summary.textContent = "Notes";
        drawnKey = null;
        if (!initialized) node.open = true;
        body.replaceChildren(
          el("p", {
            class: "error",
            text: `Could not load notebook. ${error.message}`,
          }),
          button("Retry", refresh),
        );
      })
      .finally(() => {
        pending = null;
        if (!disposed && !writing && token !== generation) refresh();
      });
    return pending;
  }
  const visible = () => {
    if (document.visibilityState === "visible") refresh();
  };
  // A route-change flush may finish after the notebook's first GET.
  const saved = () => {
    generation++;
    refresh();
  };
  document.addEventListener("agenticml-notes-saved", saved);
  document.addEventListener("visibilitychange", visible);
  window.addEventListener("resize", measureMore);
  const relativeTimer = setInterval(updateTimes, 30000);
  refresh();
  return {
    node,
    update(filter) {
      if (project === filter.project && query === filter.query) return;
      project = filter.project;
      query = filter.query;
      draw();
    },
    refresh,
    destroy() {
      disposed = true;
      abort.abort();
      clearInterval(relativeTimer);
      for (const timer of messageTimers.values()) clearTimeout(timer);
      window.removeEventListener("resize", measureMore);
      document.removeEventListener("agenticml-notes-saved", saved);
      document.removeEventListener("visibilitychange", visible);
    },
  };
}
