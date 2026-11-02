/* AgenticML local dashboard. Store strings enter the DOM only as text. */
import { MetricChart, emaSmooth, colorForId, formatNumber } from "./charts.js";
import { createCodeViewer } from "./code.js";
import { createCodeDiff } from "./codediff.js";

const app = document.querySelector("#app");
const fmt = (value) =>
  typeof value === "number"
    ? formatNumber(value)
    : value == null
      ? "—"
      : typeof value === "object"
        ? JSON.stringify(value)
        : String(value);
const date = (value) =>
  Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "—";
const apiRun = (id) => `/api/runs/${encodeURIComponent(id)}`;
const routeRun = (id) => `#/run/${encodeURIComponent(id)}`;
const objectURL = (sha, type) =>
  `/api/objects/${encodeURIComponent(sha)}?${new URLSearchParams({ type })}`;
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key in node && !key.startsWith("aria") && key !== "role")
      node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child != null));
  return node;
}
const button = (text, onclick, kind = "ghost", attrs = {}) =>
  el("button", {
    type: "button",
    class: `btn btn-${kind} btn-sm`,
    text,
    title: text,
    onclick,
    ...attrs,
  });
const chip = (text, tone = "") =>
  el("span", { class: `chip ${tone}`, text, title: text });
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
const lowerBetter = (key) => /loss|err|error/i.test(key);
// Status words are lowercase. A finished run that never logged a metric
// says so instead of looking like a clean result.
const hasMetrics = (run) =>
  run?.last_step != null ||
  (run?.keys || []).length > 0 ||
  Object.values(run?.summary || {}).some(isNumber);
const statusText = (status, run) =>
  status === "finished"
    ? run && !hasMetrics(run)
      ? "done · no metrics"
      : "done"
    : String(status || "unknown").toLowerCase();
const statusChip = (status, run) =>
  chip(
    statusText(status, run),
    { running: "accent", finished: "ok", crashed: "danger", stale: "warn" }[
      status
    ],
  );
const eyebrow = (text) => el("div", { class: "eyebrow", text });
const dot = (color) =>
  el("span", { class: "dot", style: `background:${color}` });
// "best_val_acc" reads as "best val_acc ↑"; the arrow is the better direction.
const metricLabel = (key) =>
  `${/^best_/.test(key) ? `best ${key.slice(5)}` : key} ${lowerBetter(key) ? "↓" : "↑"}`;
// Signed delta: true minus sign, and ±0 for no change.
const formatDelta = (delta) =>
  delta === 0
    ? "±0"
    : `${delta > 0 ? "+" : "−"}${formatNumber(Math.abs(delta))}`;
const deltaTone = (delta, key) =>
  delta == null || delta === 0
    ? ""
    : delta < 0 === lowerBetter(key)
      ? "ok"
      : "warn";
// Identity fields differ between any two runs, so they never count as a change.
const IDENTITY_KEYS = new Set(["name", "project", "out", "tags"]);
const HYPERPARAMETERS = ["lr", "batch_size", "epochs", "steps_per_epoch", "seed"];
function changedKeys(run, parent) {
  const keys = new Set([
    ...Object.keys(run.config || {}),
    ...Object.keys(parent.config || {}),
  ]);
  return [...keys]
    .filter(
      (key) =>
        !IDENTITY_KEYS.has(key) &&
        JSON.stringify(run.config?.[key]) !==
          JSON.stringify(parent.config?.[key]),
    )
    .sort();
}
// Root runs list their key hyperparameters (or, without any, the first params
// that vary across the project); children list only what changed.
function changedLine(run, parent, isRoot, siblingKeys = []) {
  if (isRoot) {
    const known = ["lr", "seed", "batch_size"].filter(
      (key) => key in (run.config || {}),
    );
    const shown = known.length
      ? known.map((key) => `${key}=${fmt(run.config[key])}`)
      : varied(run, siblingKeys).split(" · ").filter(Boolean).slice(0, 3);
    return ["root", ...shown].join(" · ");
  }
  const keys = changedKeys(run, parent);
  if (!keys.length) return "no param changes";
  return keys
    .map(
      (key) => `${key} ${fmt(parent.config?.[key])} → ${fmt(run.config?.[key])}`,
    )
    .join(" · ");
}
// Runs filter. Words match name, id and tags; "task:x", "tag:x" and
// "status:x" (with "failed" for crashed) narrow by task, tag and status; "key>n", "key<n", "key>=n",
// "key<=n" and "key=v" test config, then summary, values. Every token must hold.
const FILTER_COMPARISON = /^([^<>=:]+)(>=|<=|>|<|=)(.+)$/;
function runMatches(run, query) {
  const text = `${run.name} ${run.run_id} ${(run.tags || []).join(" ")}`.toLowerCase();
  return query
    .split(/\s+/)
    .filter(Boolean)
    .every((token) => {
      if (token.startsWith("task:"))
        return taskName(run).toLowerCase().includes(token.slice(5));
      if (token.startsWith("tag:"))
        return (run.tags || []).some((tag) =>
          String(tag).toLowerCase().includes(token.slice(4)),
        );
      if (token.startsWith("status:")) {
        const wanted = token.slice(7) === "failed" ? "crashed" : token.slice(7);
        return (
          String(run.status || "").toLowerCase() === wanted ||
          statusText(run.status, run).startsWith(wanted)
        );
      }
      const compared = FILTER_COMPARISON.exec(token);
      if (compared) {
        const [, name, op, raw] = compared;
        const pick = (source) =>
          Object.keys(source || {}).find((key) => key.toLowerCase() === name);
        const configKey = pick(run.config),
          summaryKey = pick(run.summary);
        if (configKey == null && summaryKey == null) return false;
        const value =
          configKey != null ? run.config[configKey] : run.summary[summaryKey];
        const number = Number(raw);
        if (op === "=")
          return String(fmt(value)).toLowerCase() === raw || value === number;
        if (!isNumber(value) || !Number.isFinite(number)) return false;
        return op === ">"
          ? value > number
          : op === "<"
            ? value < number
            : op === ">="
              ? value >= number
              : value <= number;
      }
      return text.includes(token);
    });
}
function duration(run) {
  if (!run.created_at) return "—";
  const seconds = Math.max(
    0,
    ((run.finished_at ? new Date(run.finished_at) : Date.now()) -
      new Date(run.created_at)) /
      1000,
  );
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 1) return "<1 s";
  return seconds < 60
    ? `${formatNumber(Math.round(seconds))} s`
    : seconds < 3600
      ? `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`
      : `${formatNumber(seconds / 3600)} h`;
}
function kv(data, identity = false) {
  if (!Object.keys(data || {}).length)
    return el("span", { class: "muted", text: "—" });
  return el(
    "dl",
    { class: `kv-list ${identity ? "identity" : "mono"}` },
    ...Object.entries(data || {}).map(([key, value]) =>
      el(
        "div",
        {},
        el("dt", { text: key }),
        el("dd", {}, value instanceof Node ? value : fmt(value)),
      ),
    ),
  );
}
function card(title, content, aside) {
  return el(
    "section",
    { class: "card anatomy-card", "data-card": title },
    el("header", {}, el("h2", { class: "eyebrow", text: title }), aside),
    content,
  );
}
function table(headers, rows) {
  return el(
    "div",
    { class: "table-scroll" },
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el("tr", {}, ...headers.map((text) => el("th", { text }))),
      ),
      el("tbody", {}, ...rows),
    ),
  );
}
const tableRow = (cells) =>
  el(
    "tr",
    {},
    ...cells.map((value) =>
      el("td", {}, value instanceof Node ? value : fmt(value)),
    ),
  );
let epoch = 0;
let controller;
let cleanup = () => {};
async function get(url, signal = controller?.signal) {
  const response = await fetch(url, { signal });
  if (!response.ok)
    throw new Error(
      `${response.status}: ${url.startsWith("/api/objects/") ? "Object unavailable" : await response.text()}`,
    );
  return response.json();
}
function showError(node, error) {
  if (error.name === "AbortError") return;
  const message = error.message;
  // Repeated polls must not rebuild the same alert or screen readers re-announce it.
  const current = node.querySelector(":scope > .error");
  if (current?.textContent === message) return;
  node.replaceChildren(el("p", { class: "error", role: "alert", text: message }));
}
const LOADING_ANNOUNCE_DELAY = 50;
const loading = (text) => {
  const node = el("p", { class: "muted", role: "status", "aria-live": "polite" });
  setTimeout(() => {
    node.textContent = text;
  }, LOADING_ANNOUNCE_DELAY);
  return node;
};
const pollStatusNode = () =>
  el("span", {
    class: "poll-status",
    role: "status",
    "aria-live": "polite",
  });
function setPollStatus(node, text) {
  if (node.textContent !== text) node.textContent = text;
}
let theme = "system";
try {
  const stored = localStorage.getItem("agenticml-theme");
  if (["system", "light", "dark"].includes(stored)) theme = stored;
} catch {}
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
function applyTheme() {
  document.documentElement.dataset.theme =
    theme === "system" ? (systemTheme.matches ? "dark" : "light") : theme;
  document.querySelector("#theme").textContent = `Theme: ${theme}`;
  window.dispatchEvent(new Event("dashboard-theme"));
}
document.querySelector("#theme").onclick = () => {
  theme = ["system", "light", "dark"][
    (["system", "light", "dark"].indexOf(theme) + 1) % 3
  ];
  try {
    localStorage.setItem("agenticml-theme", theme);
  } catch {}
  applyTheme();
};
systemTheme.addEventListener("change", applyTheme);
applyTheme();
// Where this dashboard is served from: always a local store.
document.querySelector("#host").textContent = `local · ${location.host}`;
document.querySelector("#close-lightbox").onclick = () =>
  document.querySelector("#lightbox").close();

// Tab and Shift+Tab wrap inside a modal host instead of leaving it.
function trapTab(host, event) {
  const targets = [
    ...host.querySelectorAll(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex]:not([tabindex="-1"])',
    ),
  ].filter((node) => {
    if (node.tabIndex < 0 || node.matches(":disabled") ||
        !node.getClientRects().length || getComputedStyle(node).visibility !== "visible")
      return false;
    for (let parent = node.parentElement; parent !== host; parent = parent.parentElement) {
      if (parent.hidden || (parent.matches("details:not([open])") &&
          !parent.querySelector(":scope > summary")?.contains(node))) return false;
    }
    return true;
  });
  const index = targets.indexOf(document.activeElement);
  if (
    index === -1 ||
    (event.shiftKey && index === 0) ||
    (!event.shiftKey && index === targets.length - 1)
  ) {
    event.preventDefault();
    (event.shiftKey ? targets.at(-1) : targets[0])?.focus();
  }
}
// Focus hosts own their keyboard handler so nested focus views close independently.
function focus(title, subtitle, className = "", onClose = () => {}) {
  const previous = document.activeElement;
  const body = el("div", { class: "focus-body" });
  const host = el("section", {
    class: `focus-overlay ${className}`,
    role: "dialog",
    "aria-modal": "true",
    "aria-label": title,
  });
  const close = () => {
    window.removeEventListener("keydown", keydown);
    host.remove();
    previous?.focus();
    onClose();
  };
  const keydown = (event) => {
    const topmost = [...document.querySelectorAll(".focus-overlay")].at(-1);
    // A workspace or lineage dialog above this view owns the keyboard.
    if (topmost !== host || document.querySelector("#lightbox[open], .ws, .lv-backdrop")) return;
    if (event.key === "Escape") {
      event.stopImmediatePropagation();
      close();
    } else if (event.key === "Tab") trapTab(host, event);
  };
  const exit = button("Exit focus", close, "secondary");
  host.append(
    el(
      "header",
      {},
      el("span", { class: "focus-label", text: "Focus" }),
      el("h1", { text: title }),
      el("span", { class: "muted", text: subtitle }),
      el(
        "div",
        { class: "exit" },
        el("kbd", { class: "muted", text: "esc" }),
        exit,
      ),
    ),
    body,
  );
  document.body.append(host);
  window.addEventListener("keydown", keydown);
  exit.focus();
  return { host, body, close };
}
function variedKeys(runs) {
  const keys = [
    ...new Set(runs.flatMap((run) => Object.keys(run.config || {}))),
  ];
  return keys
    .filter(
      (key) =>
        new Set(runs.map((run) => JSON.stringify(run.config?.[key]))).size > 1,
    )
    .sort();
}
const varied = (run, keys) =>
  keys
    .filter((key) => fmt(run.config?.[key]).length <= 40)
    .map((key) => `${key}=${fmt(run.config?.[key])}`)
    .join(" · ");
const headline = (run) => {
  const keys = Object.keys(run.summary || {}).filter((key) =>
    isNumber(run.summary[key]),
  );
  return keys.find((key) => /loss|acc/i.test(key)) || keys[0];
};
const RUN_ID_SUFFIX = /^(\d{8}T\d{6})-([0-9a-fA-F]{8})$/;
function shortRunId(id) {
  const text = String(id || "");
  const matched = RUN_ID_SUFFIX.exec(text);
  return matched ? matched[2] : text.slice(0, 8);
}
const LINEAGE_REFRESH_MS = 15000;
const LINEAGE_RETRY_MS = 1000;
const LINEAGE_RETRY_MAX_MS = 30000;
function lineageSignature(runs) {
  return runs
    .map((run) =>
      JSON.stringify([
        run.run_id, run.status || "", run.name || "", run.created_at || "",
        run.task || "", run.task_source || "", run.arch || null,
      ]),
    )
    .sort()
    .join("\n");
}
const taskName = (run) => (typeof run?.task === "string" ? run.task : "");
const createdTime = (run) => {
  const time = Date.parse(run?.created_at);
  return Number.isFinite(time) ? time : -Infinity;
};
const newestFirst = (a, b) =>
  createdTime(b) - createdTime(a) ||
  String(a.run_id).localeCompare(String(b.run_id));
// A task is judged by the first headline metric any of its runs logged.
const primaryMetric = (runs) => runs.map(headline).find(Boolean) || null;
// Smaller is better; runs without the metric sort last.
const metricScore = (run, key) => {
  const value = run?.summary?.[key];
  return key && isNumber(value) ? (lowerBetter(key) ? value : -value) : Infinity;
};
// A crashed run's partial metric never counts as the best result.
function bestRun(runs, key) {
  let best = null;
  for (const run of runs)
    if (run.status !== "crashed" && metricScore(run, key) < metricScore(best, key)) best = run;
  return best;
}
// Lineage inside one task. A parent outside the task starts a new root (an
// explicit parent_run_id may cross tasks). Siblings are ordered so the subtree
// holding the best metric keeps its parent's lane; every later sibling opens
// a new lane below.
function taskTree(runs, parentOf, metricKey) {
  const items = new Map(
    runs.map((run) => [run.run_id, { run, parent: null, kids: [], depth: 0, lane: 0 }]),
  );
  for (const item of items.values()) {
    const parent = items.get(parentOf(item.run.run_id));
    if (parent && parent !== item) {
      item.parent = parent;
      parent.kids.push(item);
    }
  }
  const scores = new Map();
  const score = (item, trail = new Set()) => {
    if (scores.has(item)) return scores.get(item);
    if (trail.has(item)) return Infinity;
    trail.add(item);
    let value = metricScore(item.run, metricKey);
    for (const kid of item.kids) value = Math.min(value, score(kid, trail));
    scores.set(item, value);
    return value;
  };
  const rank = (a, b) =>
    score(a) - score(b) ||
    createdTime(a.run) - createdTime(b.run) ||
    String(a.run.run_id).localeCompare(String(b.run.run_id));
  const order = [];
  const seen = new Set();
  let lane = -1;
  const walk = (item, depth, at) => {
    seen.add(item);
    item.depth = depth;
    item.lane = at;
    order.push(item);
    let first = true;
    for (const kid of item.kids.sort(rank)) {
      if (seen.has(kid)) continue;
      walk(kid, depth + 1, first ? at : ++lane);
      first = false;
    }
  };
  for (const root of [...items.values()].filter((item) => !item.parent).sort(rank))
    walk(root, 0, ++lane);
  // A parent cycle has no root; break it at its oldest run.
  for (const item of [...items.values()].sort((a, b) => rank(a, b))) {
    if (seen.has(item)) continue;
    item.parent = null;
    walk(item, 0, ++lane);
  }
  return {
    items,
    order,
    lanes: lane + 1,
    depth: order.reduce((max, item) => Math.max(max, item.depth + 1), 0),
  };
}
const STATUS_TONE = { finished: "ok", running: "accent", crashed: "warn", stale: "warn" };
// Mini lineage: one 7px dot per run, generations left to right, a branch
// drops to the next lane.
function lineageStrip(tree, task) {
  const x = (item) => 3 + item.depth * 22;
  const y = (item) => 3 + item.lane * 8;
  const lines = [];
  for (const item of tree.order) {
    if (!item.parent) continue;
    const px = x(item.parent) + 3,
      py = y(item.parent) + 3,
      cy = y(item) + 3;
    if (cy !== py)
      lines.push(el("span", {
        class: "task-strip-line",
        style: `left:${px}px;top:${Math.min(py, cy)}px;width:1px;height:${Math.abs(cy - py)}px`,
      }));
    lines.push(el("span", {
      class: "task-strip-line",
      style: `left:${px}px;top:${cy}px;width:${x(item) + 3 - px}px;height:1px`,
    }));
  }
  const dots = tree.order.map((item) =>
    el("span", {
      class: `task-strip-dot ${STATUS_TONE[item.run.status] || ""}`,
      title: item.run.name || item.run.run_id,
      style: `left:${x(item)}px;top:${y(item)}px`,
    }),
  );
  const runs = tree.order.length;
  return el(
    "div",
    {
      class: "task-strip",
      role: "img",
      "aria-label": `Lineage of ${task || "no task"}: ${runs} run${runs === 1 ? "" : "s"}, ${tree.depth} level${tree.depth === 1 ? "" : "s"}`,
      style: `width:${13 + Math.max(tree.depth - 1, 0) * 22}px;height:${13 + Math.max(tree.lanes - 1, 0) * 8}px`,
    },
    ...lines,
    ...dots,
  );
}
const shortTime = (time) =>
  new Date(time).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const stepText = (step) =>
  step == null
    ? "—"
    : step >= 1000
      ? `${(step / 1000).toFixed(step % 1000 && step < 10000 ? 1 : 0)}k`
      : String(step);
const metricShort = (key) => String(key || "metric").replace(/^best_/, "");
// Badge for a run in the lineage view; the best run outranks its status.
function lineageBadge(run, isBest) {
  if (isBest) return ["★ best", "ok"];
  if (run.status === "running") return ["running", "accent"];
  if (run.status === "crashed") return ["failed", "warn"];
  if (run.status === "finished") return hasMetrics(run) ? ["done", ""] : ["no metrics", ""];
  return [statusText(run.status, run), "warn"];
}
// Why a run needs a second look, from what the run list already carries.
function runWarning(run) {
  if (run.status === "crashed") {
    const code = run.summary?._exit_code;
    return `Run failed${code != null ? ` with exit code ${fmt(code)}` : ""}${run.last_step != null ? ` at step ${stepText(run.last_step)}` : ""}.`;
  }
  if (run.status === "stale") return "No heartbeat: the process may have died.";
  if (run.status === "finished" && !hasMetrics(run)) return "Finished without logging any metrics.";
  return null;
}
// Lineage view geometry: 228×112 boxes in 268px generation columns and
// 136px lanes, under a 40px row of generation labels.
const LV_W = 228;
const LV_H = 112;
const LV_COL = 268;
const LV_ROW = 136;
const LV_PAD = 20;
const LV_TOP = 40;
const TASK_SHOWN = 5;
async function home(version) {
  const selected = new Set();
  // Keys are "project\0task"; a group stays collapsed or expanded across polls.
  const collapsed = new Set();
  const showAll = new Set();
  const search = el("input", {
    type: "search",
    class: "field",
    placeholder: "task:imagegen   lr>0.1   status:failed",
    "aria-label": "Filter runs",
  });
  const tabs = el("div", {
    class: "tabs",
    role: "tablist",
    "aria-label": "Projects",
  });
  const selection = el("div", {
    class: "selection-bar is-empty",
    "data-testid": "selection-bar",
  });
  const content = el("div", { class: "stack" });
  const status = pollStatusNode();
  const count = el("p", { class: "runs-summary" });
  app.replaceChildren(
    el(
      "div",
      { class: "workspace runs-workspace stack" },
      el(
        "div",
        { class: "heading runs-heading" },
        el(
          "div",
          { class: "runs-title" },
          eyebrow("Workspace · local"),
          el("h1", { text: "Runs" }),
          count,
        ),
        el(
          "label",
          { class: "runs-filter" },
          el("span", { class: "field-label", text: "Filter runs" }),
          search,
        ),
      ),
      el(
        "div",
        { class: "tabs-row" },
        tabs,
        el("span", {
          class: "tabs-note",
          text: "Grouped by project › task · newest first",
        }),
      ),
      el("div", { class: "selection-slot" }, selection),
      content,
      status,
    ),
  );
  let runs = [],
    project = null,
    timer;
  const groupKey = (run) => `${run.project || ""}\u0000${taskName(run)}`;
  const EMPTY_SELECTION =
    "Select two or more runs to overlay their curves and diff their configs.";
  const selectionCount = el("span", { text: EMPTY_SELECTION });
  const selectionChips = el("span", { class: "selection-chips", hidden: true });
  const selectionWarn = el("span", {
    class: "selection-warn",
    text: "Different tasks — Δ isn't lineage-based",
    hidden: true,
  });
  const selectionHint = el("span", {
    class: "selection-hint",
    text: "Overlay curves and diff configs side by side.",
    hidden: true,
  });
  const compareButton = button(
    "Compare runs",
    () => {
      const query = new URLSearchParams({ selection: "1" });
      for (const id of selected) query.append("runs", id);
      location.hash = `#/compare?${query}`;
    },
    "primary",
  );
  const selectionActions = el(
    "div",
    { class: "actions", hidden: true },
    compareButton,
    button("Clear", () => {
      selected.clear();
      updateSelection();
    }),
  );
  selection.append(
    selectionCount,
    selectionChips,
    selectionWarn,
    selectionHint,
    selectionActions,
  );
  function updateSelection() {
    const picked = runs.filter((run) => selected.has(run.run_id));
    const crossTask = new Set(picked.map(groupKey)).size > 1;
    selection.classList.toggle("is-empty", !selected.size);
    selectionActions.hidden = !selected.size;
    selectionChips.hidden = !selected.size;
    selectionWarn.hidden = !crossTask;
    selectionHint.hidden = selected.size < 2 || crossTask;
    selectionCount.textContent = selected.size
      ? `${selected.size} run${selected.size === 1 ? "" : "s"} selected`
      : EMPTY_SELECTION;
    const names = picked.map((run) => run.name || run.run_id);
    if (selectionChips.dataset.names !== JSON.stringify(names)) {
      selectionChips.dataset.names = JSON.stringify(names);
      selectionChips.replaceChildren(...names.map((name) => chip(name)));
    }
    compareButton.disabled = selected.size < 2;
    for (const row of content.querySelectorAll(".run-row")) {
      const on = selected.has(row.dataset.runId);
      row.dataset.picked = on;
      row.querySelector(".check").checked = on;
    }
  }
  // One /api/lineage fetch per project, refetched when its runs change and
  // every 15 s while one is running. Failures back off 1s, 2s, 4s… to 30s,
  // independent of the runs poll.
  const lineage = new Map();
  function ensureLineage(key, siblings) {
    const signature = lineageSignature(siblings);
    let entry = lineage.get(key);
    if (!entry) {
      entry = { data: null, byId: new Map(), wanted: null, token: 0, fetchedAt: 0, retry: null, delay: 0 };
      lineage.set(key, entry);
    }
    const due =
      siblings.some((run) => run.status === "running") &&
      Date.now() - entry.fetchedAt >= LINEAGE_REFRESH_MS;
    if (entry.wanted === signature && (entry.retry || !due)) return;
    fetchLineage(key, entry, signature);
  }
  function fetchLineage(key, entry, signature) {
    clearTimeout(entry.retry);
    entry.retry = null;
    const token = ++entry.token;
    entry.wanted = signature;
    entry.fetchedAt = Date.now();
    const current = () =>
      epoch === version && entry.token === token && lineage.get(key) === entry;
    get(`/api/lineage?${new URLSearchParams({ project: key })}`)
      .then((data) => {
        if (!current()) return;
        if (!Array.isArray(data?.nodes)) throw new Error("Invalid lineage response");
        entry.data = data;
        entry.byId = new Map(data.nodes.map((node) => [node.run_id, node]));
        entry.delay = 0;
        draw();
      })
      .catch((error) => {
        if (error.name === "AbortError" || !current()) return;
        entry.delay = Math.min(
          entry.delay ? entry.delay * 2 : LINEAGE_RETRY_MS,
          LINEAGE_RETRY_MAX_MS,
        );
        entry.retry = setTimeout(() => {
          entry.retry = null;
          if (current()) fetchLineage(key, entry, entry.wanted);
        }, entry.delay);
      });
  }
  function dropLineage(key) {
    clearTimeout(lineage.get(key)?.retry);
    lineage.delete(key);
  }
  // "Changed vs parent" and "Δ vs parent" read the lineage parent. Until a
  // project's lineage answers, both cells stay "—" rather than guessing.
  function parentFacts(run, metricKey, entry, keys) {
    const unknown = { changed: "—", delta: "—", tone: "" };
    const node = entry?.byId.get(run.run_id);
    if (!node) return unknown;
    if (!node.parent_run_id)
      return { ...unknown, changed: changedLine(run, null, true, keys) };
    const parent = runs.find((other) => other.run_id === node.parent_run_id);
    if (!parent) return unknown;
    const value = run.summary?.[metricKey],
      base = parent.summary?.[metricKey];
    const delta = isNumber(value) && isNumber(base) ? value - base : null;
    return {
      changed: changedLine(run, parent, false),
      delta: delta == null ? "—" : formatDelta(delta),
      tone: deltaTone(delta, metricKey),
    };
  }
  function runRow(run, metricKey, entry, keys) {
    const check = el("input", {
      type: "checkbox",
      role: "checkbox",
      class: "check",
      checked: selected.has(run.run_id),
      "aria-label": `Select ${run.name || run.run_id}`,
      "data-focus": `check:${run.run_id}`,
      onclick: (event) => event.stopPropagation(),
      onchange: () => {
        selected.has(run.run_id)
          ? selected.delete(run.run_id)
          : selected.add(run.run_id);
        updateSelection();
      },
      onkeydown: (event) => event.stopPropagation(),
    });
    const facts = parentFacts(run, metricKey, entry, keys);
    return el(
      "div",
      {
        class: "run-grid run-row",
        "data-run-id": run.run_id,
        "data-picked": selected.has(run.run_id),
        "data-focus": `row:${run.run_id}`,
        role: "button",
        tabIndex: 0,
        onclick: () => {
          location.hash = routeRun(run.run_id);
        },
        onkeydown: (event) => {
          if (event.key === "Enter") location.hash = routeRun(run.run_id);
        },
      },
      check,
      el(
        "div",
        { class: "run-identity" },
        el(
          "div",
          { class: "inline" },
          el("a", {
            class: "run-name",
            href: routeRun(run.run_id),
            text: run.name || run.run_id,
            title: run.name || run.run_id,
            "data-focus": `name:${run.run_id}`,
            onclick: (event) => event.stopPropagation(),
          }),
          el("span", { class: "run-id", text: shortRunId(run.run_id) }),
        ),
        el("div", {
          class: "run-varied",
          text: facts.changed,
          title: facts.changed,
        }),
      ),
      el("div", { class: "run-status" }, statusChip(run.status, run)),
      el("span", { class: "muted run-progress", text: stepText(run.last_step) }),
      el("span", {
        class: "run-number run-metric",
        title: metricKey || "",
        text: fmt(run.summary?.[metricKey]),
      }),
      el("span", {
        class: `run-number run-delta ${facts.tone}`,
        text: facts.delta,
      }),
      el("span", { class: "run-number run-duration", text: duration(run) }),
      el("span", { class: "run-chevron", "aria-hidden": "true", text: "›" }),
    );
  }
  const columns = (metricKey) =>
    el(
      "div",
      { class: "run-grid run-columns" },
      el("span"),
      el("span", { text: "Run · changed vs parent" }),
      el("span", { class: "run-status", text: "Status" }),
      el("span", { class: "run-progress", text: "Last step" }),
      el("span", {
        class: "run-number run-metric",
        text: metricKey ? metricLabel(metricKey) : "Metric",
        title: metricKey || "",
      }),
      el("span", { class: "run-number run-delta", text: "Δ vs parent" }),
      el("span", { class: "run-number run-duration", text: "Wall time" }),
      el("span", { class: "run-chevron" }),
    );
  // One dashed box per (project, task): header with the task's metric and a
  // mini lineage, then its newest runs.
  function taskGroup(key, task, taskRuns, visible, entry, keys) {
    const id = `${key}\u0000${task}`;
    const open = !collapsed.has(id);
    const metricKey = primaryMetric(taskRuns);
    const best = bestRun(taskRuns, metricKey);
    const running = taskRuns.filter((run) => run.status === "running").length;
    const failed = taskRuns.filter((run) => run.status === "crashed").length;
    const finished = taskRuns
      .map((run) => Date.parse(run.finished_at))
      .filter(Number.isFinite);
    const summary = [
      `${taskRuns.length} run${taskRuns.length === 1 ? "" : "s"}`,
      running ? `${running} running` : null,
      failed ? `${failed} failed` : null,
      running
        ? "running now"
        : finished.length
          ? `last ${shortTime(Math.max(...finished))}`
          : null,
    ]
      .filter(Boolean)
      .join(" · ");
    const source = taskRuns.find((run) => run.task_source)?.task_source;
    const tree = entry?.data
      ? taskTree(taskRuns, (runId) => entry.byId.get(runId)?.parent_run_id, metricKey)
      : null;
    const head = el(
      "div",
      { class: "task-head" },
      el(
        "button",
        {
          type: "button",
          class: "task-toggle",
          "aria-expanded": String(open),
          "aria-label": `${open ? "Collapse" : "Expand"} ${task || "no task"}`,
          "data-focus": `toggle:${id}`,
          onclick: () => {
            open ? collapsed.add(id) : collapsed.delete(id);
            draw();
          },
        },
        el("span", { class: "task-caret", "aria-hidden": "true", text: open ? "▾" : "▸" }),
        el("span", { class: "task-name", text: task || "no task", title: task || "no task" }),
      ),
      task ? chip(`task · ${source || "—"}`, "task-source") : null,
      el("span", { class: "task-summary", text: summary }),
      metricKey
        ? el(
            "span",
            { class: "task-metric" },
            `${metricLabel(metricKey)} `,
            el("span", {
              class: "task-best",
              text: best ? fmt(best.summary[metricKey]) : "—",
            }),
          )
        : null,
      el(
        "div",
        { class: "task-tools" },
        tree ? lineageStrip(tree, task) : null,
        button("Lineage ⤢", () => openLineageView(key, task), "secondary", {
          "aria-haspopup": "dialog",
          "aria-label": `Open lineage of ${task || "no task"}`,
          "data-focus": `lineage:${id}`,
        }),
      ),
    );
    let body = null;
    if (open) {
      const shown = showAll.has(id) ? visible : visible.slice(0, TASK_SHOWN);
      const hidden = visible.length - TASK_SHOWN;
      body = el(
        "div",
        { class: "task-body" },
        columns(metricKey),
        ...shown.map((run) => runRow(run, metricKey, entry, keys)),
        hidden > 0
          ? el(
              "div",
              { class: "task-more" },
              button(
                showAll.has(id) ? `Show latest ${TASK_SHOWN} only` : `Show ${hidden} more`,
                () => {
                  showAll.has(id) ? showAll.delete(id) : showAll.add(id);
                  draw();
                },
                "ghost",
                { "data-focus": `more:${id}` },
              ),
            )
          : null,
      );
    }
    return el(
      "section",
      {
        class: "task-group",
        "data-task": task,
        "aria-label": `Task ${task || "no task"}`,
      },
      head,
      body,
    );
  }
  // Lineage view: a fixed dialog with the task's full graph, a compare tray
  // and an inspector for the focused run. Its selection is the table's.
  let lineageView = null;
  function openLineageView(key, task) {
    lineageView?.close(false);
    const id = `${key}\u0000${task}`;
    const label = task || "no task";
    const trigger = `lineage:${id}`;
    let zoom = 1,
      find = "",
      bestOn = true,
      focusId = null,
      first = true;
    const details = new Map();
    const findField = el("input", {
      type: "search",
      class: "field lv-find",
      placeholder: "Find run or param…",
      "aria-label": "Find run or param",
      oninput: () => {
        find = findField.value.trim().toLowerCase();
        render();
      },
    });
    const bestBox = el("input", {
      type: "checkbox",
      checked: true,
      onchange: () => {
        bestOn = bestBox.checked;
        render();
      },
    });
    const zoomValue = el("span", { class: "lv-zoom-value", text: "100%" });
    const setZoom = (value) => {
      zoom = Math.min(2, Math.max(0.3, Math.round(value * 100) / 100));
      render();
    };
    const source = el("span", { class: "lv-source" });
    const meta = el("span", { class: "lv-meta" });
    const canvas = el("div", { class: "lv-canvas-scroll" });
    const tray = el("div", { class: "lv-tray" });
    const inspector = el("aside", { class: "lv-inspector", "aria-label": "Selected run" });
    const legendDot = (tone, text) =>
      el("span", { class: "lv-legend-item" }, el("span", { class: `lv-dot ${tone}`, "aria-hidden": "true" }), text);
    const dialog = el(
      "section",
      {
        class: "lv-dialog",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": `Lineage of ${key ? `${key}/` : ""}${label}`,
      },
      el(
        "header",
        { class: "lv-head" },
        el(
          "div",
          { class: "lv-title" },
          eyebrow("Lineage"),
          el("div", { class: "inline" }, el("span", { class: "lv-task", text: label, title: label }), source),
        ),
        meta,
        el(
          "div",
          { class: "lv-controls" },
          findField,
          el("label", { class: "lv-best" }, bestBox, "Highlight best path"),
          el(
            "div",
            { class: "lv-zoom", role: "group", "aria-label": "Zoom" },
            button("−", () => setZoom(zoom - 0.1), "ghost", { "aria-label": "Zoom out", title: "Zoom out" }),
            zoomValue,
            button("+", () => setZoom(zoom + 0.1), "ghost", { "aria-label": "Zoom in", title: "Zoom in" }),
            button("Fit", () => {
              const size = canvas.firstElementChild?.firstElementChild;
              const width = parseFloat(size?.style.width) || 1,
                height = parseFloat(size?.style.height) || 1;
              setZoom(Math.min(1, (canvas.clientWidth - 16) / width, (canvas.clientHeight - 16) / height));
            }),
          ),
          button("✕", () => close(), "secondary", { "aria-label": "Close lineage", title: "Close lineage" }),
        ),
      ),
      el(
        "div",
        { class: "lv-body" },
        el(
          "div",
          { class: "lv-main" },
          canvas,
          tray,
          el(
            "div",
            { class: "lv-legend" },
            legendDot("ok", "done"),
            legendDot("accent", "running"),
            legendDot("warn", "failed"),
            el("span", { class: "lv-legend-item" }, el("span", { class: "lv-legend-line is-best", "aria-hidden": "true" }), "best path"),
            el("span", { class: "lv-legend-item" }, el("span", { class: "lv-legend-line", "aria-hidden": "true" }), "parent → child"),
            el("span", {
              class: "lv-footnote",
              text: "Parents are inferred within a task only. An explicit parent_run_id can cross tasks.",
            }),
          ),
        ),
        inspector,
      ),
    );
    const backdrop = el("div", {
      class: "lv-backdrop",
      onclick: (event) => {
        if (event.target === backdrop) close();
      },
    }, dialog);
    const keydown = (event) => {
      if (document.querySelector(".focus-overlay")) return;
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        close();
      } else if (event.key === "Tab") trapTab(dialog, event);
    };
    const toggle = (runId) => {
      selected.has(runId) ? selected.delete(runId) : selected.add(runId);
      updateSelection();
      render();
    };
    function close(restore = true) {
      window.removeEventListener("keydown", keydown);
      backdrop.remove();
      document.documentElement.classList.remove("has-dialog");
      if (lineageView === view) lineageView = null;
      if (!restore) return;
      const target = [...content.querySelectorAll("[data-focus]")].find(
        (node) => node.dataset.focus === trigger,
      );
      target?.focus();
    }
    function render() {
      const taskRuns = runs
        .filter((run) => (run.project || "") === key && taskName(run) === task)
        .sort(newestFirst);
      if (!taskRuns.length) return close();
      const entry = lineage.get(key);
      zoomValue.textContent = `${Math.round(zoom * 100)}%`;
      const taskSource = taskRuns.find((run) => run.task_source)?.task_source;
      source.replaceChildren(...(task ? [chip(`task · ${taskSource || "—"}`, "task-source")] : []));
      if (!entry?.data) {
        if (!canvas.querySelector("[role=status]"))
          canvas.replaceChildren(loading("Loading lineage…"));
        return;
      }
      const active = dialog.contains(document.activeElement)
        ? document.activeElement.closest("[data-focus]")?.dataset.focus
        : null;
      const metricKey = primaryMetric(taskRuns);
      const short = metricShort(metricKey);
      const keys = variedKeys(runs.filter((run) => (run.project || "") === key));
      const tree = taskTree(taskRuns, (runId) => entry.byId.get(runId)?.parent_run_id, metricKey);
      const best = bestRun(taskRuns, metricKey);
      const path = [];
      for (let item = best && tree.items.get(best.run_id); item; item = item.parent)
        path.unshift(item.run.run_id);
      const onPath = new Set(bestOn ? path : []);
      if (!tree.items.has(focusId)) focusId = (best || tree.order[0].run).run_id;
      const running = taskRuns.filter((run) => run.status === "running").length;
      const failed = taskRuns.filter((run) => run.status === "crashed").length;
      meta.textContent = [
        `${taskRuns.length} run${taskRuns.length === 1 ? "" : "s"}`,
        `${tree.depth} generation${tree.depth === 1 ? "" : "s"}`,
        `${tree.lanes} branch${tree.lanes === 1 ? "" : "es"}`,
        running ? `${running} running` : null,
        failed ? `${failed} failed` : null,
        best ? `best ${best.name || best.run_id} (${short} ${fmt(best.summary[metricKey])})` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      // Find matches names, ids, tags and filter expressions, plus param names.
      const matches = (run) =>
        !find ||
        runMatches(run, find) ||
        Object.keys(run.config || {}).some((name) => name.toLowerCase().includes(find));
      const x = (item) => LV_PAD + item.depth * LV_COL;
      const y = (item) => LV_TOP + item.lane * LV_ROW;
      const width = LV_PAD * 2 + Math.max(tree.depth - 1, 0) * LV_COL + LV_W;
      const height = LV_TOP + Math.max(tree.lanes - 1, 0) * LV_ROW + LV_H + LV_PAD;
      const parts = [];
      for (let depth = 0; depth < tree.depth; depth++)
        parts.push(el("span", {
          class: "lv-gen",
          style: `left:${LV_PAD + depth * LV_COL}px`,
          text: depth ? `gen ${depth}` : "root",
        }));
      // Elbow connectors: out of the parent's right edge, down the gutter,
      // into the child's left edge with an arrowhead.
      for (const item of tree.order) {
        if (!item.parent) continue;
        const hot = onPath.has(item.run.run_id) && onPath.has(item.parent.run.run_id);
        const dim = find && !(matches(item.run) && matches(item.parent.run));
        const tone = `lv-edge${hot ? " is-best" : ""}${dim ? " is-dim" : ""}`;
        const px = x(item.parent) + LV_W,
          py = y(item.parent) + LV_H / 2,
          cx = x(item),
          cy = y(item) + LV_H / 2,
          mx = px + (LV_COL - LV_W) / 2;
        const segment = (left, top, w, h) =>
          parts.push(el("span", { class: tone, style: `left:${left}px;top:${top}px;width:${w}px;height:${h}px` }));
        const thick = hot ? 2 : 1;
        segment(px, py, mx - px, thick);
        if (cy !== py) segment(mx, Math.min(py, cy), thick, Math.abs(cy - py) + thick);
        segment(mx, cy, cx - mx, thick);
        parts.push(el("span", { class: `${tone} lv-arrow`, style: `left:${cx - 5}px;top:${cy - 4 + thick / 2}px` }));
      }
      for (const item of tree.order) {
        const run = item.run;
        const runId = run.run_id;
        const name = run.name || runId;
        const facts = parentFacts(run, metricKey, entry, keys);
        const [badge, badgeTone] = lineageBadge(run, best?.run_id === runId);
        const ticked = selected.has(runId);
        const classes = [
          "lv-node",
          run.status === "crashed" || run.status === "stale" ? "is-failed" : "",
          run.status === "running" ? "is-running" : "",
          onPath.has(runId) ? "is-best-path" : "",
          ticked ? "is-ticked" : "",
          runId === focusId ? "is-focused" : "",
          matches(run) ? "" : "is-dim",
        ].filter(Boolean).join(" ");
        parts.push(el(
          "div",
          { class: classes, "data-run-id": runId, style: `left:${x(item)}px;top:${y(item)}px` },
          el(
            "button",
            {
              type: "button",
              class: "lv-box",
              "aria-pressed": String(runId === focusId),
              "aria-label": `${name}, ${statusText(run.status, run)}, ${short} ${fmt(run.summary?.[metricKey])}`,
              "data-focus": `lv-node:${runId}`,
              onclick: () => {
                focusId = runId;
                render();
              },
              ondblclick: () => {
                location.hash = routeRun(runId);
              },
            },
            el(
              "span",
              { class: "lv-box-top" },
              el("span", { class: `lv-dot ${STATUS_TONE[run.status] || ""}`, "aria-hidden": "true" }),
              el("span", { class: "lv-name", text: name, title: name }),
              chip(badge, `lv-badge ${badgeTone}`),
            ),
            el("span", {
              class: `lv-change${item.parent ? "" : " is-root"}`,
              text: item.parent ? facts.changed : "root run",
              title: item.parent ? facts.changed : "root run",
            }),
            el(
              "span",
              { class: "lv-box-metric" },
              el("span", { class: "lv-metric-key", text: short }),
              el("span", { class: "lv-metric-value", text: fmt(run.summary?.[metricKey]) }),
              item.parent ? el("span", { class: `lv-delta ${facts.tone}`, text: facts.delta }) : null,
              el("span", { class: "lv-foot", text: `${stepText(run.last_step)} steps · ${duration(run)}` }),
            ),
          ),
          el("input", {
            type: "checkbox",
            class: "lv-tick",
            checked: ticked,
            "aria-label": `Select ${name} for comparison`,
            "data-focus": `lv-tick:${runId}`,
            onchange: () => toggle(runId),
          }),
        ));
      }
      const top = canvas.scrollTop,
        left = canvas.scrollLeft;
      canvas.replaceChildren(
        el(
          "div",
          { class: "lv-sizer", style: `width:${width * zoom}px;height:${height * zoom}px` },
          el("div", { class: "lv-canvas", style: `width:${width}px;height:${height}px;transform:scale(${zoom})` }, ...parts),
        ),
      );
      canvas.scrollTop = top;
      canvas.scrollLeft = left;
      drawTray(taskRuns, tree, path);
      drawInspector(tree, entry, metricKey, keys);
      const target = active &&
        [...dialog.querySelectorAll("[data-focus]")].find((node) => node.dataset.focus === active);
      if (first) {
        first = false;
        const box = canvas.querySelector(`.lv-node.is-focused .lv-box`);
        box?.focus({ preventScroll: true });
        box?.scrollIntoView({ block: "nearest", inline: "nearest" });
      } else if (active && !dialog.contains(document.activeElement)) target?.focus({ preventScroll: true });
    }
    // Ticks are scoped to this task but live in the page-wide selection.
    function setTaskSelection(taskRuns, ids) {
      for (const run of taskRuns) selected.delete(run.run_id);
      for (const runId of ids) selected.add(runId);
      updateSelection();
      render();
    }
    function drawTray(taskRuns, tree, path) {
      const ticked = tree.order.map((item) => item.run).filter((run) => selected.has(run.run_id));
      const focused = tree.items.get(focusId);
      const family = [
        focused.run.run_id,
        ...(focused.parent ? [focused.parent.run.run_id] : []),
        ...focused.kids.map((kid) => kid.run.run_id),
      ];
      const query = new URLSearchParams({ selection: "1" });
      for (const run of ticked) query.append("runs", run.run_id);
      tray.classList.toggle("is-ready", ticked.length >= 2);
      tray.replaceChildren(
        el("span", {
          class: ticked.length >= 2 ? "lv-tray-count" : "lv-tray-hint",
          text: ticked.length >= 2
            ? `${ticked.length} selected in ${label}`
            : "Tick two or more runs in this task to compare them.",
        }),
        el(
          "div",
          { class: "lv-tray-chips" },
          ...ticked.map((run) =>
            el(
              "button",
              {
                type: "button",
                class: "lv-chip",
                "aria-label": `Remove ${run.name || run.run_id} from comparison`,
                "data-focus": `lv-chip:${run.run_id}`,
                onclick: () => toggle(run.run_id),
              },
              el("span", { class: "lv-chip-name", text: run.name || run.run_id }),
              el("span", { "aria-hidden": "true", text: "✕" }),
            ),
          ),
        ),
        el(
          "div",
          { class: "lv-tray-actions" },
          el("span", { class: "lv-tray-label", text: "Quick select" }),
          button("Best path", () => setTaskSelection(taskRuns, path), "secondary", {
            disabled: !path.length,
            "data-focus": "lv-pick-path",
          }),
          button(
            `${focused.run.name || focused.run.run_id} + parent & children`,
            () => setTaskSelection(taskRuns, family),
            "secondary",
            { class: "btn btn-secondary btn-sm lv-family", "data-focus": "lv-pick-family" },
          ),
          button("Clear", () => setTaskSelection(taskRuns, []), "ghost", { "data-focus": "lv-clear" }),
          ticked.length >= 2
            ? el("a", {
                class: "btn btn-primary btn-sm",
                href: `#/compare?${query}`,
                text: `Compare ${ticked.length} runs`,
              })
            : null,
        ),
      );
    }
    function drawInspector(tree, entry, metricKey, keys) {
      const item = tree.items.get(focusId);
      const run = item.run;
      const runId = run.run_id;
      const node = entry.byId.get(runId) || {};
      const parent = runs.find((other) => other.run_id === node.parent_run_id) || null;
      let root = item;
      while (root.parent) root = root.parent;
      const facts = parentFacts(run, metricKey, entry, keys);
      const value = run.summary?.[metricKey],
        base = root.run.summary?.[metricKey];
      const vsRoot = root !== item && isNumber(value) && isNumber(base) ? value - base : null;
      const short = metricShort(metricKey);
      const warning = runWarning(run);
      const diff = parent
        ? changedKeys(run, parent).map((name) => [name, fmt(parent.config?.[name]), fmt(run.config?.[name])])
        : Object.keys(run.config || {})
            .filter((name) => !IDENTITY_KEYS.has(name))
            .sort()
            .map((name) => [name, "—", fmt(run.config[name])]);
      // Changed files come from the per-run lineage diff, fetched once.
      if (parent && !node.same_tree_as_parent && !details.has(runId)) {
        details.set(runId, null);
        get(`${apiRun(runId)}/lineage`)
          .then((detail) => {
            details.set(runId, detail);
            if (lineageView === view && focusId === runId) render();
          })
          .catch(() => details.set(runId, { error: true }));
      }
      const detail = details.get(runId);
      const files = Array.isArray(detail?.files) ? detail.files : null;
      const code = !parent
        ? "baseline snapshot"
        : node.same_tree_as_parent
          ? "code unchanged"
          : files
            ? `${files.length} file${files.length === 1 ? "" : "s"} changed${files[0] ? ` · ${files[0].path}` : ""}`
            : "code changed";
      const stat = (name, text, tone = "") =>
        el("div", {}, el("dt", { text: name }), el("dd", { class: tone, text }));
      inspector.replaceChildren(
        el(
          "div",
          { class: "lv-insp-head" },
          el(
            "div",
            { class: "inline" },
            el("span", { class: `lv-dot ${STATUS_TONE[run.status] || ""}`, "aria-hidden": "true" }),
            el("span", { class: "lv-insp-name", text: run.name || runId, title: run.name || runId }),
            statusChip(run.status, run),
          ),
          el("span", {
            class: "lv-insp-sub",
            text: `run ${shortRunId(runId)} · ${parent ? `child of ${parent.name || parent.run_id}` : `root of ${label}`} · gen ${item.depth}`,
          }),
          el(
            "label",
            { class: "lv-include" },
            el("input", {
              type: "checkbox",
              checked: selected.has(runId),
              "data-focus": "lv-include",
              onchange: () => toggle(runId),
            }),
            "Include in comparison",
          ),
          el(
            "div",
            { class: "inline lv-insp-actions" },
            el("a", { class: "btn btn-primary btn-sm", href: routeRun(runId), text: "Open run" }),
            parent
              ? el("a", {
                  class: "btn btn-secondary btn-sm",
                  href: `#/compare?${new URLSearchParams([["runs", parent.run_id], ["runs", runId]])}`,
                  text: "Compare with parent",
                })
              : null,
          ),
        ),
        el(
          "div",
          { class: "lv-insp-section" },
          el(
            "dl",
            { class: "lv-stats" },
            stat(short, fmt(value)),
            stat("Δ vs parent", parent ? facts.delta : "—", facts.tone),
            stat("Δ vs root", vsRoot == null ? "—" : formatDelta(vsRoot), deltaTone(vsRoot, metricKey)),
          ),
          warning ? el("p", { class: "callout callout-warn", role: "note", text: warning }) : null,
        ),
        el(
          "div",
          { class: "lv-insp-section" },
          eyebrow(parent ? "Config diff vs parent" : "Root config"),
          diff.length
            ? el(
                "div",
                { class: "lv-diff" },
                el("span", { text: "param" }),
                el("span", { text: parent ? parent.name || parent.run_id : "—", title: parent?.name || "" }),
                el("span", { text: "this run" }),
                ...diff.flatMap(([name, from, to]) => [
                  el("span", { class: "lv-diff-key", text: name, title: name }),
                  el("span", { class: "lv-diff-from", text: from, title: from }),
                  el("span", { class: "lv-diff-to", text: to, title: to }),
                ]),
              )
            : el("p", { class: "muted", text: parent ? "No param changes." : "No params logged." }),
        ),
        el(
          "div",
          { class: "lv-insp-section" },
          kv({
            snapshot: node.commit ? String(node.commit).slice(0, 7) : "—",
            code,
            "last step": stepText(run.last_step),
            "wall time": duration(run),
          }),
        ),
        el(
          "div",
          { class: "lv-insp-section" },
          eyebrow(item.kids.length ? `Children · ${item.kids.length}` : "Children"),
          ...(item.kids.length
            ? item.kids.map((kid) =>
                el(
                  "button",
                  {
                    type: "button",
                    class: "lv-kid",
                    "data-focus": `lv-kid:${kid.run.run_id}`,
                    onclick: () => {
                      focusId = kid.run.run_id;
                      render();
                      canvas.querySelector(".lv-node.is-focused")?.scrollIntoView({ block: "nearest", inline: "nearest" });
                    },
                  },
                  el("span", { class: `lv-dot ${STATUS_TONE[kid.run.status] || ""}`, "aria-hidden": "true" }),
                  el("span", { class: "lv-kid-name", text: kid.run.name || kid.run.run_id }),
                  el("span", { class: "lv-kid-change", text: changedLine(kid.run, run, false) }),
                  el("span", { class: "lv-kid-metric", text: fmt(kid.run.summary?.[metricKey]) }),
                ),
              )
            : [el("p", { class: "muted", text: "No child runs yet." })]),
        ),
      );
    }
    const view = { key, task, render, close };
    lineageView = view;
    document.body.append(backdrop);
    document.documentElement.classList.add("has-dialog");
    window.addEventListener("keydown", keydown);
    render();
    if (!dialog.contains(document.activeElement)) findField.focus();
  }
  // Polls rebuild the groups; keyboard focus follows the control it was on.
  function focusKey() {
    const active = document.activeElement;
    return content.contains(active) ? active.closest("[data-focus]")?.dataset.focus : null;
  }
  function restoreFocus(key) {
    if (!key || content.contains(document.activeElement)) return;
    const target = [...content.querySelectorAll("[data-focus]")].find(
      (node) => node.dataset.focus === key,
    );
    try {
      target?.focus({ preventScroll: true });
    } catch {
      target?.focus();
    }
  }
  function projectCard(key, siblings, query) {
    const entry = lineage.get(key);
    const keys = variedKeys(siblings);
    const tasks = new Map();
    for (const run of siblings) {
      const task = taskName(run);
      if (!tasks.has(task)) tasks.set(task, []);
      tasks.get(task).push(run);
    }
    // Newest activity first; runs without a task close the card.
    const ordered = [...tasks].sort(([a, aRuns], [b, bRuns]) =>
      !a ? 1 : !b ? -1 : createdTime(bRuns[0]) - createdTime(aRuns[0]) || a.localeCompare(b),
    );
    const groups = [];
    for (const [task, taskRuns] of ordered) {
      const visible = taskRuns.filter((run) => runMatches(run, query));
      if (query && !visible.length) continue;
      groups.push(taskGroup(key, task, taskRuns, visible, entry, keys));
    }
    const ids = ordered.map(([task]) => `${key}\u0000${task}`);
    const allCollapsed = ids.every((id) => collapsed.has(id));
    const named = ordered.filter(([task]) => task).length;
    const meta = [
      named ? `${named} task${named === 1 ? "" : "s"}` : null,
      `${siblings.length} run${siblings.length === 1 ? "" : "s"}`,
    ]
      .filter(Boolean)
      .join(" · ");
    let card = [...content.children].find(
      (node) => node.classList.contains("project-card") && node.dataset.project === key,
    );
    card ||= el("section", { class: "card project-card", "data-project": key });
    card.replaceChildren(
      el(
        "header",
        { class: "project-head inline" },
        el("h2", { text: key || "No project" }),
        el("span", { class: "project-meta", text: meta }),
        !key
          ? el("span", {
              class: "project-note",
              text: "no project — pass project= or run inside a repo",
            })
          : null,
        button(
          allCollapsed ? "Expand all" : "Collapse all",
          () => {
            for (const id of ids) allCollapsed ? collapsed.delete(id) : collapsed.add(id);
            draw();
          },
          "ghost",
          { class: "btn btn-ghost btn-sm project-toggle", "data-focus": `all:${key}` },
        ),
      ),
      el(
        "div",
        { class: "task-groups" },
        ...(groups.length
          ? groups
          : [el("p", { class: "runs-empty muted", text: "No runs match this filter." })]),
      ),
    );
    return { card, matched: groups.length > 0 };
  }
  function draw() {
    const focused = focusKey();
    const groups = new Map();
    for (const run of runs) {
      const key = run.project || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(run);
    }
    const projects = [...groups.keys()].sort((a, b) =>
      !a ? -1 : !b ? 1 : a.localeCompare(b),
    );
    if (project !== null && !groups.has(project)) project = null;
    const shown = projects.filter((key) => project === null || key === project);
    for (const key of [...lineage.keys()])
      if (!shown.includes(key)) dropLineage(key);
    for (const tab of tabs.querySelectorAll("button")) tab.remove();
    tabs.prepend(
      ...[null, ...projects].map((key) => {
        const label = key === null ? "All projects" : key || "No project";
        const tab = button(
          label,
          () => {
            project = key;
            draw();
          },
          "ghost",
          { role: "tab", "aria-selected": key === project, "aria-label": label },
        );
        tab.append(
          el("span", {
            class: "tab-count",
            "aria-hidden": "true",
            text: key === null ? runs.length : groups.get(key).length,
          }),
        );
        return tab;
      }),
    );
    const taskCount = new Set(
      runs.filter((run) => taskName(run)).map(groupKey),
    ).size;
    const running = runs.filter((run) => run.status === "running").length;
    count.textContent = [
      `${runs.length} run${runs.length === 1 ? "" : "s"}`,
      `${projects.length} project${projects.length === 1 ? "" : "s"}`,
      `${taskCount} task${taskCount === 1 ? "" : "s"}`,
      running ? `${running} running` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    const query = search.value.trim().toLowerCase();
    const cards = [];
    for (const key of shown) {
      const siblings = groups.get(key).sort(newestFirst);
      ensureLineage(key, siblings);
      const { card, matched } = projectCard(key, siblings, query);
      if (matched || !query) cards.push(card);
    }
    if (!cards.length) {
      content.replaceChildren(
        el("p", {
          class: "empty",
          text: runs.length
            ? "No runs match this filter."
            : "No runs found. Start a local experiment to see it here.",
        }),
      );
    } else {
      const wanted = new Set(cards);
      for (const child of [...content.children])
        if (!wanted.has(child)) child.remove();
      let cursor = content.firstChild;
      for (const card of cards) {
        if (cursor === card) {
          cursor = cursor.nextSibling;
          continue;
        }
        content.insertBefore(card, cursor);
      }
    }
    updateSelection();
    restoreFocus(focused);
    lineageView?.render();
  }
  search.oninput = draw;
  async function refresh() {
    try {
      const result = await get("/api/runs");
      if (epoch !== version) return;
      runs = result;
      for (const id of selected)
        if (!runs.some((run) => run.run_id === id)) selected.delete(id);
      draw();
      const live = runs.some((run) => run.status === "running");
      setPollStatus(
        status,
        live ? "Live · refreshes every 5 s" : `${runs.length} runs`,
      );
      if (live) timer = setTimeout(refresh, 5000);
    } catch (error) {
      if (epoch === version) {
        showError(content, error);
        timer = setTimeout(refresh, 5000);
      }
    }
  }
  cleanup = () => {
    clearTimeout(timer);
    lineageView?.close(false);
    for (const key of [...lineage.keys()]) dropLineage(key);
  };
  await refresh();
}

async function loadMetrics(run, signal) {
  const keys = run.keys || [];
  const chunks = [];
  for (let i = 0; i < keys.length; i += 200)
    chunks.push(keys.slice(i, i + 200));
  if (!chunks.length) chunks.push([]);
  const results = await Promise.all(
    chunks.map((chunk) => {
      const query = new URLSearchParams({ max_points: "1000" });
      chunk.forEach((key) => query.append("key", key));
      return get(`${apiRun(run.run_id)}/scalars?${query}`, signal);
    }),
  );
  const metrics = Object.create(null);
  for (const result of results)
    for (const [key, series] of Object.entries(result.series || {})) {
      metrics[key] = (series.points || [])
        .map((point) => ({
          step:
            series.mode === "overview" ? (point[0] + point[1]) / 2 : point[0],
          value: series.mode === "overview" ? point[4] : point[1],
        }))
        .filter((point) => isNumber(point.step))
        .map((point) => ({
          ...point,
          value: isNumber(point.value) ? point.value : null,
        }))
        .sort((a, b) => a.step - b.step);
    }
  return { metrics, truncated: results.some((result) => result.truncated) };
}
// Metric key filter: plain text matches anywhere; "*" is a wildcard
// ("val/*", "*loss").
function keyFilter(text) {
  const query = text.trim().toLowerCase();
  if (!query.includes("*")) return (key) => key.toLowerCase().includes(query);
  const pattern = new RegExp(
    `^${query.split("*").map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*")}$`,
  );
  return (key) => pattern.test(key.toLowerCase());
}
// Direction words for chart headers; keys that are neither stay unlabelled.
const metricDirection = (key) =>
  lowerBetter(key)
    ? "lower is better"
    : /acc|f1|auc|precision|recall|score|reward|iou|bleu/i.test(key)
      ? "higher is better"
      : "";
const EPOCH_KEY = /(^|\/)epoch$/i;
// x-axis mappers built from logged data only: an epoch series, or the
// (step, seconds) flush records in _time.bin. null when the run has neither.
function axisMapper(axis, runMetrics, records) {
  if (axis === "epoch") {
    const key = Object.keys(runMetrics || {}).find((name) => EPOCH_KEY.test(name));
    const samples = (runMetrics?.[key] || []).filter((point) => isNumber(point.value));
    if (!samples.length) return null;
    return (step) => {
      let value = samples[0].value;
      for (const sample of samples) {
        if (sample.step > step) break;
        value = sample.value;
      }
      return value;
    };
  }
  if (axis === "wall time") {
    if (!Array.isArray(records) || records.length < 2) return null;
    const points = [...records].sort((a, b) => a[0] - b[0]);
    if (points[0][0] > 0) points.unshift([0, 0]);
    // Steps between two flushes are placed linearly between their times.
    return (step) => {
      if (step >= points.at(-1)[0]) return points.at(-1)[1];
      for (let i = 1; i < points.length; i++)
        if (step <= points[i][0]) {
          const [s0, t0] = points[i - 1],
            [s1, t1] = points[i];
          return s1 === s0 ? t1 : t0 + ((step - s0) / (s1 - s0)) * (t1 - t0);
        }
      return points[0][1];
    };
  }
  return (step) => step;
}
function metricsPanel(initialRuns, compare, signal, { onMetrics, workspace: place = {} } = {}) {
  const node = el("div", { class: "metrics-panel stack" });
  let runs = initialRuns,
    metrics = Object.create(null),
    charts = new Map(),
    disposed = false;
  // A single run plots in --s1. In a comparison the first run is the
  // reference (dashed --s5); the others take --s1…--s4 in order.
  const colors = Object.fromEntries(
    runs.map((run, index) => [
      run.run_id,
      !compare ? "var(--s1)" : index === 0 ? "var(--s5)" : `var(--s${((index - 1) % 4) + 1})`,
    ]),
  );
  const reference = compare ? runs[0]?.run_id : null;
  let xAxis = "step";
  const timeIndex = new Map();
  const axisSelect = el(
    "select",
    { class: "field axis-select", "aria-label": "x-axis" },
    ...["step", "epoch", "wall time"].map((value) =>
      el("option", { value, text: value }),
    ),
  );
  const axisReasons = {
    epoch: "No epoch series logged (e.g. epoch or auto/epoch)",
    "wall time": "Needs at least two scalar flush times",
  };
  function refreshAxes() {
    for (const option of axisSelect.options) {
      if (option.value === "step") continue;
      const usable = runs.some((run) =>
        axisMapper(option.value, metrics[run.run_id], timeIndex.get(run.run_id)),
      );
      option.disabled = !usable;
      option.title = usable ? "" : axisReasons[option.value];
    }
    if (axisSelect.selectedOptions[0]?.disabled) {
      axisSelect.value = xAxis = "step";
    }
  }
  // Points re-keyed onto the chosen x-axis; "at" keeps the logged step.
  let viewCache = null;
  function view() {
    if (xAxis === "step") return metrics;
    if (viewCache?.axis === xAxis && viewCache.metrics === metrics && viewCache.times === timeIndex.size)
      return viewCache.view;
    const mapped = Object.create(null);
    for (const run of runs) {
      const map = axisMapper(xAxis, metrics[run.run_id], timeIndex.get(run.run_id));
      mapped[run.run_id] = Object.create(null);
      if (!map) continue;
      for (const [key, points] of Object.entries(metrics[run.run_id] || {}))
        mapped[run.run_id][key] = points.map((point) => ({
          step: map(point.step),
          value: point.value,
          at: point.step,
        }));
    }
    viewCache = { axis: xAxis, metrics, times: timeIndex.size, view: mapped };
    return mapped;
  }
  const chartRuns = () => runs.map((run) => ({ ...run, id: run.run_id }));
  const slider = el("input", {
    type: "range",
    min: 0,
    max: 0.99,
    step: 0.01,
    value: 0.6,
    "aria-label": "EMA smoothing",
  });
  const smoothValue = el("span", { class: "mono", text: "0.60" });
  const outliers = el("input", {
    type: "checkbox",
    checked: true,
    "aria-label": "Clip outliers on y-axis",
  });
  const filter = el("input", {
    class: "field",
    type: "search",
    placeholder: "Filter metric keys — val/*",
    "aria-label": "Filter metric keys",
  });
  const count = el("span", { class: "muted" }),
    content = el("div", { class: "stack" }),
    note = el("div", { class: "muted" }),
    failure = el("div", { role: "alert" });
  // Joined error text from the last paint. update() runs on every poll.
  let failureText = "";
  node.append(
    el(
      "div",
      { class: "card toolbar" },
      el("label", {}, eyebrow("EMA smoothing"), slider, smoothValue),
      el("label", {}, outliers, "Clip outliers on y-axis"),
      el("label", {}, el("span", { class: "eyebrow", text: "x-axis" }), axisSelect),
      filter,
      count,
    ),
  );
  if (runs.length > 1)
    node.append(
      el(
        "div",
        { class: "legend" },
        ...runs.map((run) =>
          el(
            "span",
            { class: `inline run-label${run.run_id === reference ? " is-reference" : ""}` },
            dot(colors[run.run_id]),
            run.name || run.run_id,
          ),
        ),
      ),
    );
  node.append(failure, content, note);
  const options = (key) => ({
    runs: chartRuns(),
    metrics: view(),
    activeMetric: key,
    smoothing: Number(slider.value),
    ignoreOutliers: outliers.checked,
    colors,
    xLabel: xAxis === "wall time" ? "wall time (s)" : xAxis,
    ghostRaw: !compare,
    dashed: reference ? [reference] : [],
  });
  const metricCache = new Map();
  // Metrics workspace: every scalar chart in one full-window view, with the
  // other runs of the task to overlay. Smoothing and outlier scaling are this
  // panel's own controls, so both views always agree.
  let workspace = null;
  function openWorkspace(focusKey = null) {
    workspace?.close(false);
    const trigger = document.activeElement;
    const primary = runs[0]?.run_id;
    const parentId = place.parent?.() || null;
    const state = {
      filter: "",
      columns: 2,
      axis: "step",
      sort: "default",
      runFilter: "",
      collapsed: new Set(),
      wide: new Set(focusKey ? [focusKey] : []),
      log: new Set(),
    };
    let candidates = runs.slice(),
      version = 0,
      closed = false,
      wsCharts = [];
    const checked = new Set(runs.map((run) => run.run_id));
    const loaded = new Map();
    const times = new Map();
    const smoothedCache = new Map();
    // This run --s1, its parent --s2, the rest --s3… in list order; a
    // comparison keeps the panel's reference colours.
    const colorOf = (run) => {
      if (compare) return colors[run.run_id] || colorForId(run.run_id);
      const index = candidates.findIndex((other) => other.run_id === run.run_id);
      return `var(--s${(Math.max(index, 0) % 5) + 1})`;
    };
    const hasScalars = (run) =>
      run.last_step != null || (run.keys || []).length > 0 || Boolean(metrics[run.run_id]);
    const shown = () =>
      candidates.filter((run) => checked.has(run.run_id) && loaded.has(run.run_id));
    const relative = (run) =>
      axisMapper("wall time", loaded.get(run.run_id), times.get(run.run_id));
    const started = (run) => Date.parse(run.created_at) / 1000;
    const usable = (axis) =>
      axis === "step" ||
      shown().some((run) => relative(run) && (axis === "relative" || Number.isFinite(started(run))));
    // Seconds as "42 s", "3m 5s", "1.2 h"; wall clock as local time.
    const seconds = (value) =>
      !isNumber(value)
        ? "—"
        : value < 60
          ? `${formatNumber(Math.round(value * 10) / 10)} s`
          : value < 3600
            ? `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`
            : `${formatNumber(value / 3600)} h`;
    const clock = (value) => new Date(value * 1000).toLocaleTimeString();
    let viewCache = null;
    function view() {
      if (viewCache?.version === version && viewCache.axis === state.axis) return viewCache.view;
      const mapped = Object.create(null);
      for (const run of shown()) {
        const data = loaded.get(run.run_id) || {};
        const rel = relative(run);
        const map =
          state.axis === "step"
            ? null
            : state.axis === "relative"
              ? rel
              : rel && Number.isFinite(started(run))
                ? (step) => started(run) + rel(step)
                : null;
        mapped[run.run_id] = Object.create(null);
        if (state.axis !== "step" && !map) continue;
        for (const [key, points] of Object.entries(data))
          mapped[run.run_id][key] = map
            ? points.map((point) => ({ step: map(point.step), value: point.value, at: point.step }))
            : points;
      }
      viewCache = { version, axis: state.axis, view: mapped };
      return mapped;
    }
    function smoothedValues(run, key) {
      const raw = loaded.get(run.run_id)?.[key] || [];
      const id = `${run.run_id}\u0000${key}`;
      const cached = smoothedCache.get(id);
      if (cached?.raw === raw && cached.weight === slider.value) return cached.values;
      const values = emaSmooth(raw.map((point) => point.value), Number(slider.value));
      smoothedCache.set(id, { raw, weight: slider.value, values });
      return values;
    }
    // Epoch boundaries: where the first shown run's epoch series steps up.
    function epochLines() {
      const run = shown()[0];
      if (!run) return [];
      const data = loaded.get(run.run_id) || {};
      const key = Object.keys(data).find((name) => EPOCH_KEY.test(name));
      const points = view()[run.run_id]?.[key] || [];
      const lines = [];
      for (let i = 1; i < points.length; i++)
        if (points[i].value !== points[i - 1].value) lines.push(points[i].step);
      return lines;
    }
    const chartOptions = (key) => ({
      runs: shown().map((run) => ({ ...run, id: run.run_id })),
      metrics: view(),
      activeMetric: key,
      smoothing: Number(slider.value),
      ignoreOutliers: outliers.checked,
      colors: Object.fromEntries(shown().map((run) => [run.run_id, colorOf(run)])),
      xLabel: state.axis === "step" ? "step" : state.axis === "relative" ? "relative time" : "wall time",
      xFormat: state.axis === "relative" ? seconds : state.axis === "wall" ? clock : null,
      rawAlpha: 0.3,
      log: state.log.has(key),
      epochs: epochLines(),
      snap: true,
      dashed: compare && reference ? [reference] : [],
    });
    const filterField = el("input", {
      type: "search",
      class: "field ws-filter",
      placeholder: "Filter tags — e.g. loss, val/",
      "aria-label": "Filter tags",
      oninput: () => {
        state.filter = filterField.value;
        drawMain();
      },
    });
    const columnButtons = [1, 2, 3].map((n) =>
      button(String(n), () => {
        state.columns = n;
        drawMain();
      }, "ghost", { "aria-label": `${n} column${n === 1 ? "" : "s"}`, title: `${n} column${n === 1 ? "" : "s"}` }),
    );
    const summary = el("span", { class: "ws-meta" });
    const smoothValueWs = el("span", { class: "mono", text: Number(slider.value).toFixed(2) });
    const smoothing = el("input", {
      type: "range",
      min: 0,
      max: 0.99,
      step: 0.01,
      value: slider.value,
      "aria-label": "Smoothing",
      oninput: () => {
        slider.value = smoothing.value;
        smoothValueWs.textContent = Number(smoothing.value).toFixed(2);
        slider.oninput();
      },
    });
    const outliersWs = el("input", {
      type: "checkbox",
      checked: outliers.checked,
      onchange: () => {
        outliers.checked = outliersWs.checked;
        outliers.onchange();
      },
    });
    const axisButtons = [["step", "Step"], ["relative", "Relative"], ["wall", "Wall"]].map(([axis, label]) =>
      button(label, () => {
        state.axis = axis;
        drawMain();
      }, "ghost", { "data-axis": axis }),
    );
    const sortSelect = el(
      "select",
      { class: "field", onchange: () => (state.sort = sortSelect.value) },
      el("option", { value: "default", text: "default (run order)" }),
      el("option", { value: "descending", text: "descending value" }),
      el("option", { value: "nearest", text: "nearest to cursor" }),
    );
    const runFilter = el("input", {
      type: "search",
      class: "field",
      placeholder: "Filter runs (regex)",
      "aria-label": "Filter runs",
      oninput: () => {
        state.runFilter = runFilter.value;
        drawRuns();
      },
    });
    const runList = el("div", { class: "ws-runs" });
    const main = el("div", { class: "ws-main" });
    const crumbs = place.crumbs?.() || (compare ? ["compare"] : [runs[0]?.name || primary || "run"]);
    const host = el(
      "section",
      {
        class: "ws",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": `Metrics workspace for ${crumbs.at(-1)}`,
      },
      el(
        "header",
        { class: "ws-head" },
        el("span", { class: "ws-crumbs", text: `agenticml / ${crumbs.join(" / ")} /`, title: crumbs.join(" / ") }),
        el("h2", { class: "ws-title", text: "Metrics" }),
        summary,
        el(
          "div",
          { class: "ws-controls" },
          filterField,
          el("div", { class: "ws-segment", role: "group", "aria-label": "Columns" },
            el("span", { class: "ws-segment-label", text: "Columns" }), ...columnButtons),
          button("Close ✕", () => close(), "ghost", { "aria-label": "Close metrics workspace" }),
        ),
      ),
      el(
        "div",
        { class: "ws-body" },
        el(
          "aside",
          { class: "ws-side", "aria-label": "Metrics settings" },
          el(
            "div",
            { class: "ws-side-group" },
            eyebrow("Settings"),
            el("label", { class: "ws-field" },
              el("span", { class: "ws-field-row" }, "Smoothing", smoothValueWs), smoothing),
            el("label", { class: "ws-check" }, outliersWs, "Ignore outliers in chart scaling"),
            el("div", { class: "ws-field" },
              el("span", { text: "Horizontal axis" }),
              el("div", { class: "ws-segment", role: "group", "aria-label": "Horizontal axis" }, ...axisButtons)),
            el("label", { class: "ws-field" }, "Tooltip sorting", sortSelect),
          ),
          el(
            "div",
            { class: "ws-side-group" },
            el("div", { class: "inline" }, eyebrow("Runs"),
              el("span", { class: "ws-runs-scope", text: compare ? "this comparison" : place.scope?.() || "" })),
            runFilter,
            runList,
          ),
        ),
        main,
      ),
    );
    function drawRuns() {
      let pattern = null;
      try {
        pattern = new RegExp(state.runFilter, "i");
      } catch {}
      const matches = (run) => {
        const name = run.name || run.run_id;
        return !state.runFilter || (pattern
          ? pattern.test(name)
          : name.toLowerCase().includes(state.runFilter.toLowerCase()));
      };
      const listed = candidates.filter(matches);
      runList.replaceChildren(
        ...(listed.length
          ? listed.map((run) => {
              const name = run.name || run.run_id;
              const has = hasScalars(run);
              const role = run.run_id === primary && !compare
                ? "this run"
                : run.run_id === parentId
                  ? "parent"
                  : `run ${shortRunId(run.run_id)}`;
              return el(
                "label",
                { class: `ws-run${has ? "" : " is-disabled"}`, "data-run-id": run.run_id },
                el("input", {
                  type: "checkbox",
                  checked: has && checked.has(run.run_id),
                  disabled: !has,
                  "aria-label": `Show ${name}`,
                  onchange: (event) => toggleRun(run, event.target.checked),
                }),
                el("span", { class: "ws-swatch", style: `background:${colorOf(run)}`, "aria-hidden": "true" }),
                el(
                  "span",
                  { class: "ws-run-text" },
                  el("span", { class: "ws-run-name", text: name, title: name }),
                  el("span", {
                    class: "ws-run-meta",
                    text: has ? `${role} · ${stepText(run.last_step)} steps` : `${role} · no scalars`,
                  }),
                ),
              );
            })
          : [el("p", { class: "muted", text: "No runs match." })]),
      );
    }
    async function toggleRun(run, on) {
      if (on) checked.add(run.run_id);
      else checked.delete(run.run_id);
      if (on) await load([run]);
      if (closed) return;
      version++;
      drawMain();
    }
    // The panel's own runs reuse its series; other runs load on first tick.
    async function load(list) {
      await Promise.all(list.map(async (run) => {
        if (metrics[run.run_id]) {
          loaded.set(run.run_id, metrics[run.run_id]);
          times.set(run.run_id, timeIndex.get(run.run_id));
          return;
        }
        if (loaded.has(run.run_id)) return;
        try {
          const [result, time] = await Promise.all([
            loadMetrics(run, signal),
            get(`${apiRun(run.run_id)}/time`, signal).catch(() => []),
          ]);
          loaded.set(run.run_id, result.metrics);
          times.set(run.run_id, Array.isArray(time) ? time : []);
        } catch (error) {
          if (error.name !== "AbortError") checked.delete(run.run_id);
        }
      }));
    }
    function tagMatcher() {
      const text = state.filter.trim();
      if (!text) return () => true;
      try {
        const pattern = new RegExp(text, "i");
        return (key) => pattern.test(key);
      } catch {
        return (key) => key.toLowerCase().includes(text.toLowerCase());
      }
    }
    function showTip(card, key, x, info) {
      const tip = card.querySelector(".ws-tip");
      if (x == null) {
        tip.hidden = true;
        return;
      }
      const rows = shown().flatMap((run, order) => {
        const points = view()[run.run_id]?.[key] || [];
        if (!points.length) return [];
        let i = 0;
        points.forEach((point, index) => {
          if (Math.abs(point.step - x) < Math.abs(points[i].step - x)) i = index;
        });
        const step = points[i].at ?? points[i].step;
        return [{
          run,
          order,
          smoothed: smoothedValues(run, key)[i],
          value: points[i].value,
          step,
          relative: relative(run)?.(step),
        }];
      });
      const rank = (row) => (isNumber(row.smoothed) ? row.smoothed : -Infinity);
      if (state.sort === "descending") rows.sort((a, b) => rank(b) - rank(a) || a.order - b.order);
      if (state.sort === "nearest")
        rows.sort((a, b) =>
          Math.abs((isNumber(a.smoothed) ? a.smoothed : Infinity) - info.value) -
            Math.abs((isNumber(b.smoothed) ? b.smoothed : Infinity) - info.value) || a.order - b.order);
      tip.replaceChildren(
        el("span"), el("span", { text: "Run" }), el("span", { text: "Smoothed" }),
        el("span", { text: "Value" }), el("span", { text: "Step" }), el("span", { text: "Relative" }),
        ...rows.flatMap((row) => [
          el("span", { class: "ws-swatch", style: `background:${colorOf(row.run)}` }),
          el("span", { class: "ws-tip-name", text: row.run.name || row.run.run_id }),
          el("span", { class: "ws-tip-strong", text: fmt(row.smoothed) }),
          el("span", { text: fmt(row.value) }),
          el("span", { text: fmt(row.step) }),
          el("span", { text: seconds(row.relative) }),
        ]),
      );
      tip.hidden = false;
      const box = card.querySelector(".ws-chart").getBoundingClientRect();
      const width = tip.offsetWidth,
        height = tip.offsetHeight;
      let left = info.clientX - box.left + 14;
      if (left + width > box.width) left = Math.max(0, info.clientX - box.left - width - 14);
      const top = Math.min(Math.max(0, info.clientY - box.top + 14), Math.max(0, box.height - height));
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    }
    function download(key) {
      const quote = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
      const lines = [["run", "run_id", "step", "value", "smoothed"].join(",")];
      for (const run of shown()) {
        const raw = loaded.get(run.run_id)?.[key] || [];
        const smooth = smoothedValues(run, key);
        raw.forEach((point, i) =>
          lines.push([quote(run.name || run.run_id), quote(run.run_id), point.step, point.value ?? "", smooth[i] ?? ""].join(",")));
      }
      const url = URL.createObjectURL(new Blob([`${lines.join("\n")}\n`], { type: "text/csv" }));
      const link = el("a", { href: url, download: `${key.replace(/[^\w.-]+/g, "_")}.csv` });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }
    function drawMain() {
      for (const chart of wsCharts) chart.destroy();
      wsCharts = [];
      if (!usable(state.axis)) state.axis = "step";
      for (const node of axisButtons) {
        node.setAttribute("aria-pressed", String(node.dataset.axis === state.axis));
        node.disabled = !usable(node.dataset.axis);
        node.title = node.disabled ? "Needs at least two scalar flush times" : node.textContent;
      }
      columnButtons.forEach((node, index) =>
        node.setAttribute("aria-pressed", String(index + 1 === state.columns)));
      const visible = shown();
      const keys = [...new Set(visible.flatMap((run) => Object.keys(loaded.get(run.run_id) || {})))].sort();
      const tags = keys.filter(tagMatcher());
      summary.textContent = `${visible.length} run${visible.length === 1 ? "" : "s"} · ${tags.length} / ${keys.length} tags`;
      const groups = new Map();
      for (const key of tags) {
        const group = key.includes("/") ? key.slice(0, key.indexOf("/")) : "metrics";
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(key);
      }
      const pending = [];
      const sections = [...groups.entries()]
        .sort(([a], [b]) => (a === "_system" ? 1 : b === "_system" ? -1 : a.localeCompare(b)))
        .map(([group, groupKeys]) => {
          const open = !state.collapsed.has(group);
          const label = group === "metrics" ? group : `${group}/`;
          return el(
            "section",
            { class: "ws-group", "data-group": group },
            el(
              "button",
              {
                type: "button",
                class: "ws-group-toggle",
                "aria-expanded": String(open),
                onclick: () => {
                  open ? state.collapsed.add(group) : state.collapsed.delete(group);
                  drawMain();
                },
              },
              el("span", { class: "task-caret", "aria-hidden": "true", text: open ? "▾" : "▸" }),
              el("span", { class: "ws-group-name", text: label }),
              el("span", { class: "ws-group-count", text: `${groupKeys.length} tag${groupKeys.length === 1 ? "" : "s"}` }),
            ),
            open
              ? el(
                  "div",
                  { class: "ws-grid", style: `grid-template-columns:repeat(${state.columns}, minmax(0, 1fr))` },
                  ...groupKeys.map((key) => {
                    const wide = state.wide.has(key);
                    const canvas = el("canvas", { "aria-label": `Metric chart ${key}` });
                    const card = el(
                      "div",
                      { class: `card ws-card${wide ? " is-wide" : ""}`, "data-key": key },
                      el(
                        "header",
                        { class: "ws-card-head" },
                        el("span", { class: "ws-card-key", text: key, title: key }),
                        metricDirection(key) ? el("span", { class: "chart-dir", text: metricDirection(key) }) : null,
                        el(
                          "div",
                          { class: "ws-card-tools" },
                          button(wide ? "⤡" : "⤢", () => {
                            wide ? state.wide.delete(key) : state.wide.add(key);
                            drawMain();
                          }, "ghost", {
                            "aria-label": `${wide ? "Shrink" : "Enlarge"} ${key}`,
                            title: wide ? "Shrink" : "Enlarge",
                            "data-focus": `ws-wide:${key}`,
                          }),
                          button("log", () => {
                            state.log.has(key) ? state.log.delete(key) : state.log.add(key);
                            drawMain();
                          }, "ghost", {
                            "aria-pressed": String(state.log.has(key)),
                            "aria-label": `Log scale for ${key}`,
                            title: "Log scale",
                            "data-focus": `ws-log:${key}`,
                          }),
                          button("⭳", () => download(key), "ghost", {
                            "aria-label": `Download ${key} as CSV`,
                            title: "Download CSV",
                          }),
                        ),
                      ),
                      el("div", { class: "ws-chart" }, canvas, el("div", { class: "ws-tip", hidden: true })),
                    );
                    pending.push({ key, canvas, card });
                    return card;
                  }),
                )
              : null,
          );
        });
      const active = document.activeElement?.dataset?.focus;
      main.replaceChildren(
        ...(sections.length
          ? sections
          : [el("p", {
              class: "ws-empty",
              text: !visible.length ? "No runs selected." : keys.length ? "No tags match the filter." : "No scalars logged yet.",
            })]),
      );
      for (const { key, canvas, card } of pending)
        wsCharts.push(new MetricChart(canvas, {
          ...chartOptions(key),
          onHover: (x, info) => showTip(card, key, x, info),
        }));
      if (active) [...main.querySelectorAll("[data-focus]")].find((node) => node.dataset.focus === active)?.focus();
    }
    function repaint() {
      const options = { smoothing: Number(slider.value), ignoreOutliers: outliers.checked };
      for (const chart of wsCharts) chart.update(options);
      smoothing.value = slider.value;
      smoothValueWs.textContent = Number(slider.value).toFixed(2);
      outliersWs.checked = outliers.checked;
    }
    // Polls refresh the panel's runs in place.
    function refresh() {
      let changed = false;
      for (const run of runs)
        if (metrics[run.run_id] && loaded.get(run.run_id) !== metrics[run.run_id]) {
          loaded.set(run.run_id, metrics[run.run_id]);
          times.set(run.run_id, timeIndex.get(run.run_id));
          changed = true;
        }
      if (!changed) return;
      version++;
      drawMain();
    }
    const keydown = (event) => {
      if (document.querySelector("#lightbox[open]")) return;
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        close();
      } else if (event.key === "Tab") trapTab(host, event);
    };
    function close(restore = true) {
      if (closed) return;
      closed = true;
      for (const chart of wsCharts) chart.destroy();
      window.removeEventListener("keydown", keydown);
      host.remove();
      document.documentElement.classList.remove("has-dialog");
      if (workspace === handle) workspace = null;
      if (restore && trigger?.isConnected) trigger.focus();
    }
    const handle = { close, repaint, refresh };
    workspace = handle;
    document.body.append(host);
    document.documentElement.classList.add("has-dialog");
    window.addEventListener("keydown", keydown);
    drawRuns();
    summary.textContent = "Loading…";
    (async () => {
      const listed = await Promise.resolve(place.runs?.() || runs).catch(() => runs);
      if (closed) return;
      // The panel's runs stay first and carry their full detail.
      candidates = [
        ...runs,
        ...listed.filter((run) => !runs.some((own) => own.run_id === run.run_id)),
      ];
      if (parentId && candidates.some((run) => run.run_id === parentId && hasScalars(run)))
        checked.add(parentId);
      drawRuns();
      await load(candidates.filter((run) => checked.has(run.run_id)));
      if (closed) return;
      version++;
      drawRuns();
      drawMain();
      const target = focusKey && [...main.querySelectorAll(".ws-card")].find((card) => card.dataset.key === focusKey);
      if (target) {
        target.scrollIntoView({ block: "nearest" });
        target.querySelector(".ws-card-tools .btn")?.focus({ preventScroll: true });
      } else if (!host.contains(document.activeElement)) filterField.focus();
    })();
    filterField.focus();
  }
  let chartLayout = null;
  function updateValues(key, chartCard) {
    chartCard.querySelector(".chart-values").replaceChildren(
      ...runs.map((run) => {
        const points = metrics[run.run_id]?.[key] || [];
        return el(
          "span",
          {
            class: "inline",
            "data-run": run.run_id,
            "data-points": points.length,
          },
          dot(colors[run.run_id]),
          fmt(points.at(-1)?.value),
        );
      }),
    );
    // One run: the header says which step the shown value is from.
    if (!compare) {
      const last = (metrics[runs[0]?.run_id]?.[key] || []).at(-1);
      const text = last ? `@ step ${fmt(last.step)} ⤢` : "⤢";
      const corner = chartCard.querySelector(".chart-key");
      if (corner.textContent !== text) corner.textContent = text;
    }
  }
  function draw() {
    const pending = [];
    const keys = [
      ...new Set(runs.flatMap((run) => Object.keys(metrics[run.run_id] || {}))),
    ].sort();
    const shown = keys.filter(keyFilter(filter.value));
    count.textContent = `${shown.length} / ${keys.length} keys`;
    const layout = JSON.stringify(shown);
    if (chartLayout === layout && charts.size) {
      for (const [key, chart] of charts) {
        updateValues(key, chart.canvas.closest(".chart-card"));
        chart.update({ ...options(key), compact: true });
      }
      workspace?.refresh();
      return;
    }
    chartLayout = layout;
    const groups = new Map();
    for (const key of shown) {
      const group = key.includes("/")
        ? key.slice(0, key.indexOf("/"))
        : "metrics";
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(key);
    }
    content.replaceChildren(
      ...[...groups.entries()]
        .sort(([a], [b]) =>
          a === "_system" ? 1 : b === "_system" ? -1 : a.localeCompare(b),
        )
        .map(([group, groupKeys]) => {
          const grid = el("div", { class: "chart-grid" });
          for (const key of groupKeys) {
            const existing = charts.get(key);
            const canvas =
              existing?.canvas ||
              el("canvas", {
                "aria-label": `Metric chart ${key}`,
              });
            const chartCard =
              existing?.canvas.closest(".chart-card") ||
              el(
                "button",
                {
                  type: "button",
                  class: "card chart-card",
                  "data-key": key,
                  "aria-label": `Open ${key} in the metrics workspace`,
                  onclick: () => openWorkspace(key),
                },
                el(
                  "header",
                  {},
                  el(
                    "span",
                    { class: "chart-title" },
                    el("span", { class: "chart-name", text: key }),
                    metricDirection(key)
                      ? el("span", { class: "chart-dir", text: metricDirection(key) })
                      : null,
                  ),
                  el("span", { class: "chart-key", text: "⤢" }),
                ),
                el("div", { class: "chart-host" }, canvas),
                el(
                  "div",
                  { class: "chart-values" },
                  ...runs.map((run) => {
                    const points = metrics[run.run_id]?.[key] || [];
                    return el(
                      "span",
                      {
                        class: "inline",
                        "data-run": run.run_id,
                        "data-points": points.length,
                      },
                      dot(colors[run.run_id]),
                      fmt(points.at(-1)?.value),
                    );
                  }),
                ),
              );
            updateValues(key, chartCard);
            grid.append(chartCard);
            pending.push({ key, canvas, existing });
          }
          return el(
            "section",
            { class: "stack", "data-metric-group": group },
            el(
              "div",
              { class: "inline" },
              el("h3", { text: group === "metrics" ? group : `${group}/` }),
              el("span", {
                class: "muted",
                text: `${groupKeys.length} key${groupKeys.length === 1 ? "" : "s"}`,
              }),
            ),
            grid,
          );
        }),
    );
    // Construct and paint only after the canvases are attached and measurable.
    for (const { key, canvas, existing } of pending) {
      if (existing) existing.update({ ...options(key), compact: true });
      else
        charts.set(
          key,
          new MetricChart(canvas, { ...options(key), compact: true }),
        );
    }
    const shownSet = new Set(shown);
    for (const [key, chart] of charts) {
      if (!shownSet.has(key)) {
        chart.destroy();
        charts.delete(key);
      }
    }
    if (!shown.length)
      content.append(
        el("p", {
          class: "muted",
          text: keys.length
            ? "No metric keys match this filter."
            : "No metrics logged yet.",
        }),
      );
    workspace?.refresh();
  }
  slider.oninput = () => {
    smoothValue.textContent = Number(slider.value).toFixed(2);
    charts.forEach((chart) =>
      chart.update({ smoothing: Number(slider.value) }),
    );
    workspace?.repaint();
  };
  outliers.onchange = () => {
    charts.forEach((chart) =>
      chart.update({ ignoreOutliers: outliers.checked }),
    );
    workspace?.repaint();
  };
  filter.oninput = draw;
  axisSelect.onchange = () => {
    xAxis = axisSelect.value;
    viewCache = null;
    draw();
  };
  async function update(nextRuns = runs) {
    runs = nextRuns;
    const signatures = runs.map((run) =>
      JSON.stringify([run.last_step, [...(run.keys || [])].sort()]),
    );
    const results = await Promise.allSettled(
      runs.map((run, i) => {
        const cached = metricCache.get(run.run_id);
        return cached?.signature === signatures[i]
          ? cached.result
          : loadMetrics(run, signal);
      }),
    );
    if (disposed || signal.aborted) return;
    const errors = [];
    let truncated = false,
      changed = false;
    results.forEach((result, i) => {
      if (result.status === "fulfilled") {
        changed ||= metrics[runs[i].run_id] !== result.value.metrics;
        metrics[runs[i].run_id] = result.value.metrics;
        metricCache.set(runs[i].run_id, {
          signature: signatures[i],
          result: result.value,
        });
        truncated ||= result.value.truncated;
      } else if (result.reason.name !== "AbortError")
        errors.push(
          `${runs[i].name || runs[i].run_id}: ${result.reason.message}`,
        );
    });
    // Repeated polls must not rebuild the same alert or screen readers re-announce it.
    if (errors.length) {
      const nextFailure = errors.join("\n");
      if (nextFailure !== failureText) {
        failureText = nextFailure;
        failure.replaceChildren(
          ...errors.map((text) => el("p", { class: "error", text })),
        );
      }
    } else if (failureText) {
      failureText = "";
      failure.replaceChildren();
    }
    note.textContent = truncated
      ? "Some metric keys were truncated by the server."
      : "";
    // Flush times back the wall-time axis; tests and old stores may lack them.
    const times = await Promise.all(
      runs.map((run) =>
        get(`${apiRun(run.run_id)}/time`, signal).catch(() => []),
      ),
    );
    if (disposed || signal.aborted) return;
    let timesChanged = false;
    runs.forEach((run, i) => {
      const records = Array.isArray(times[i]) ? times[i] : [];
      if (JSON.stringify(timeIndex.get(run.run_id)) !== JSON.stringify(records)) {
        timeIndex.set(run.run_id, records);
        timesChanged = true;
      }
    });
    refreshAxes();
    if (changed || timesChanged || !charts.size) draw();
    onMetrics?.(metrics);
  }
  content.append(loading("Loading metrics…"));
  return {
    node,
    update,
    openWorkspace: () => openWorkspace(),
    destroy() {
      disposed = true;
      charts.forEach((chart) => chart.destroy());
      workspace?.close(false);
    },
  };
}

function mediaCard(media) {
  const content = el("div", { class: "stack" });
  for (const [key, items] of Object.entries(media || {})) {
    if (!Array.isArray(items)) continue;
    const strip = el("div", { class: "media-strip" });
    for (const item of [...items].sort((a, b) => a.step - b.step)) {
      if (
        !/^image\/(png|jpeg|gif|webp|avif|bmp|x-icon)$/.test(item.content_type)
      )
        continue;
      const src = objectURL(item.sha256, item.content_type);
      const image = el("img", {
        src,
        loading: "lazy",
        alt: item.caption || key,
        tabIndex: 0,
        role: "button",
        "aria-label": `Expand ${item.caption || key}`,
      });
      const enlarge = () => {
        const dialog = document.querySelector("#lightbox");
        dialog.querySelector("img").src = src;
        dialog.querySelector("img").alt = item.caption || key;
        dialog.showModal();
      };
      image.onclick = enlarge;
      image.onkeydown = (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          enlarge();
        }
      };
      strip.append(
        el(
          "figure",
          {},
          image,
          el("figcaption", {
            text: `Step ${item.step}${item.caption ? ` · ${item.caption}` : ""}`,
          }),
        ),
      );
    }
    if (strip.children.length) content.append(el("h3", { text: key }), strip);
  }
  if (!content.children.length)
    content.append(el("span", { class: "muted", text: "No media logged." }));
  const node = card("Media", content);
  node.classList.add("media-card");
  return node;
}
function identity(run) {
  const git = run.git || {};
  return kv(
    {
      "Run id": run.run_id,
      Project: run.project || "No project",
      Started: date(run.created_at),
      Finished: date(run.finished_at),
      Host: run.hostname || "—",
      Commit: el(
        "span",
        { class: "inline mono git-identity" },
        el("span", { text: `${git.git_commit?.slice(0, 8) || "—"} ·` }),
        el("span", { text: git.git_branch || "—" }),
        git.is_dirty ? chip("dirty", "warn") : null,
      ),
      "Exit code": run.config?.exit_code ?? run.summary?._exit_code ?? "—",
    },
    true,
  );
}
async function runPage(id, version) {
  const signal = controller.signal;
  let [detail, summaries, initialMedia] = await Promise.all([
    get(apiRun(id), signal),
    get("/api/runs", signal),
    get(`${apiRun(id)}/media`, signal),
  ]);
  if (epoch !== version) return;
  let media = initialMedia;
  let run = {
    ...detail,
    last_step: summaries.find((item) => item.run_id === id)?.last_step,
    run_id: id,
    status: summaries.find((run) => run.run_id === id)?.status || detail.status,
  };
  let siblings = summaries.filter(
    (other) => (other.project || "") === (run.project || ""),
  );
  // The lineage parent comes with the run; its config/summary come from the index.
  // An explicit top-level parent wins over the snapshot's inferred one.
  const parentId = () => run.parent_run_id || run.lineage?.parent_run_id || null;
  const parentOf = () => summaries.find((other) => other.run_id === parentId());
  const title = el("div", { class: "inline run-title" }),
    subline = el("span", { class: "subline" }),
    changeLabel = el("span", { class: "change-label" }),
    changeNote = el("span", { class: "change-note" }),
    strip = el("div", { class: "change-strip" }, changeLabel, subline, changeNote),
    stats = el("dl", { class: "stat-row" }),
    facts = el("div", { "data-testid": "identity" }),
    anatomy = el("div", { class: "anatomy" }),
    outputs = el("div", { class: "anatomy outputs" }),
    reproNote = el("span", { class: "muted" }),
    snapshotNote = el("span", { class: "muted" });
  // Parent snapshot tree, for "code unchanged"; fetched once when there is a parent.
  let parentTree;
  let lastValues = {};
  const pollStatus = pollStatusNode(),
    pollError = el("div");
  const codeContainer = el(
    "div",
    { class: "code-container" },
    loading("Loading code snapshot…"),
  );
  const gitAside = el("span", { class: "muted", text: "Finding parent…" });
  const parameterSearch = el("input", {
    class: "field",
    type: "search",
    placeholder: "Filter parameters…",
    "aria-label": "Search Parameters",
  });
  const hyperValues = el("div"),
    parameterValues = el("div");
  const filterParameters = () => {
    const query = parameterSearch.value.toLowerCase();
    for (const host of [hyperValues, parameterValues])
      for (const row of host.querySelectorAll(".kv-list > div")) {
        row.hidden = !row.textContent.toLowerCase().includes(query);
      }
  };
  let entrypoint = null;
  parameterSearch.oninput = filterParameters;
  let code = null,
    timer;
  // The workspace overlays the other runs of this run's task: parent first,
  // then newest first.
  const taskOfRun = () => taskName(summaries.find((item) => item.run_id === id));
  const panel = metricsPanel([run], false, signal, {
    onMetrics: (metrics) => {
      lastValues = metrics[id] || {};
      drawStats();
    },
    workspace: {
      crumbs: () => [run.project || "no project", taskOfRun() || "no task", run.name || id],
      scope: () => `task ${taskOfRun() || "none"}`,
      parent: parentId,
      runs: () =>
        summaries
          .filter((other) => (other.project || "") === (run.project || "") && taskName(other) === taskOfRun())
          .sort((a, b) => (b.run_id === parentId()) - (a.run_id === parentId()) || newestFirst(a, b)),
    },
  });
  // Headline numbers: the project's primary metric, final loss curves, steps
  // and wall time. Δ vs parent wherever the parent logged the same summary key.
  function drawStats() {
    const parent = parentOf();
    const items = [];
    const stat = (label, value, delta, key, note) =>
      items.push(
        el(
          "div",
          { class: "stat" },
          el("dt", { text: label }),
          el("dd", { class: "stat-value", text: value }),
          delta != null
            ? el("dd", {
                class: `stat-delta run-delta ${deltaTone(delta, key)}`,
                text: `${formatDelta(delta)} vs parent`,
              })
            : note
              ? el("dd", { class: "stat-delta", text: note })
              : null,
        ),
      );
    const primary = headline(run);
    if (primary) {
      const base = parent?.summary?.[primary];
      stat(
        primary.replace(/^best_/, "best ").replace(/^final_/, "final "),
        fmt(run.summary[primary]),
        isNumber(base) ? run.summary[primary] - base : null,
        primary,
      );
    }
    for (const key of Object.keys(lastValues).filter(lowerBetter).sort().slice(0, 2)) {
      const point = [...lastValues[key]].reverse().find((p) => isNumber(p.value));
      if (point) stat(`final ${key}`, fmt(point.value));
    }
    if (run.last_step != null) {
      const steps = run.last_step + 1;
      const { epochs, steps_per_epoch: per } = run.config || {};
      stat(
        "steps",
        fmt(steps),
        null,
        "",
        isNumber(epochs) && isNumber(per) && epochs * per === steps
          ? `${epochs} epochs × ${per}`
          : null,
      );
    }
    stat("wall time", duration(run));
    stats.replaceChildren(...items);
  }
  function drawFacts() {
    const parent = parentOf();
    title.replaceChildren(
      el("h1", { class: "mono", text: run.name || id }),
      statusChip(run.status, run),
      ...(run.tags || []).map((tag) => chip(tag)),
      parent
        ? el(
            "span",
            { class: "run-parent" },
            "child of ",
            el("a", { href: routeRun(parent.run_id), text: parent.name || parent.run_id }),
          )
        : null,
      el(
        "div",
        { class: "run-actions" },
        parent
          ? el("a", {
              class: "btn btn-secondary btn-sm",
              href: `#/compare?${new URLSearchParams([["selection", "1"], ["runs", parent.run_id], ["runs", id]])}`,
              text: "Compare with parent",
            })
          : null,
      ),
    );
    const root = run.lineage && !parentId();
    changeLabel.textContent = parent ? "changed vs parent" : root ? "root run" : "params";
    subline.textContent = parent
      ? changedLine(run, parent, false)
      : root
        ? changedLine(run, null, true, variedKeys(siblings))
        : varied(run, variedKeys(siblings)) || "—";
    subline.title = subline.textContent;
    const tree = run.lineage?.tree;
    changeNote.textContent = parent
      ? [
          changedKeys(run, parent).length ? "everything else inherited" : null,
          parentTree === undefined || !tree
            ? null
            : parentTree === tree
              ? "code unchanged"
              : "code changed",
        ]
          .filter(Boolean)
          .map((text) => `· ${text}`)
          .join(" ")
      : "";
    if (parent && parentTree === undefined) {
      parentTree = null;
      get(apiRun(parent.run_id), signal)
        .then((detail) => {
          if (epoch !== version) return;
          parentTree = detail.lineage?.tree || null;
          drawFacts();
        })
        .catch(() => {});
    }
    drawStats();
    facts.replaceChildren(identity(run));
    reproNote.textContent = `Everything needed to re-run ${run.name || id} exactly.`;
    snapshotNote.textContent = `${run.git?.git_commit ? `@ ${run.git.git_commit.slice(0, 7)} · ` : ""}read-only`;
    const env = { ...(run.env || {}) },
      packages = env.packages;
    delete env.packages;
    const environment = el("div", { class: "stack" }, kv(env));
    if (packages)
      environment.append(
        el(
          "details",
          {},
          el("summary", { text: "Packages (pip freeze)" }),
          el("pre", {
            text:
              typeof packages === "object"
                ? JSON.stringify(packages, null, 2)
                : String(packages),
          }),
        ),
      );
    const git = run.git || {};
    // One row per artifact version, newest first; the newest carries "latest".
    const versions = [...(run.artifacts || [])].sort(
      (a, b) =>
        String(a.name || "").localeCompare(String(b.name || "")) ||
        (b.version ?? 0) - (a.version ?? 0),
    );
    const newest = new Map();
    for (const artifact of versions)
      if (!newest.has(artifact.name)) newest.set(artifact.name, artifact);
    const artifacts = el(
      "div",
      { class: "stack" },
      ...versions.map((artifact) =>
        el(
          "div",
          { class: "inline", "data-artifact": artifact.name },
          el("span", {
            class: "mono",
            text: `${artifact.name || artifact.path || "artifact"}${artifact.version == null ? "" : `:v${artifact.version}`}`,
          }),
          artifact.version != null && newest.get(artifact.name) === artifact
            ? chip("latest", "accent")
            : null,
          el("span", {
            class: "muted",
            text: [
              artifact.type,
              artifact.size == null ? "—" : formatBytes(artifact.size),
            ]
              .filter(Boolean)
              .join(" · "),
          }),
        ),
      ),
    );
    if (!artifacts.children.length)
      artifacts.append(el("span", { class: "muted", text: "—" }));
    const changed = new Set(parent ? changedKeys(run, parent) : []);
    const config = run.config || {};
    const marked = (key) =>
      changed.has(key)
        ? el("span", { class: "inline" }, chip("changed", "accent"), fmt(config[key]))
        : config[key];
    const hyper = Object.fromEntries(
      HYPERPARAMETERS.filter((key) => key in config).map((key) => [key, marked(key)]),
    );
    const rest = Object.fromEntries(
      Object.keys(config)
        .filter((key) => !HYPERPARAMETERS.includes(key))
        .map((key) => [key, marked(key)]),
    );
    hyperValues.replaceChildren(kv(hyper));
    parameterValues.replaceChildren(kv(rest));
    filterParameters();
    // Δ only for logged results; counts echoed from the config are not metrics.
    const summary = Object.fromEntries(
      Object.entries(run.summary || {}).map(([key, value]) => {
        const base = parent?.summary?.[key];
        const delta =
          isNumber(value) && isNumber(base) && !(key in config) ? value - base : null;
        return [
          key,
          delta == null
            ? value
            : el(
                "span",
                { class: "inline summary-value" },
                fmt(value),
                el("span", {
                  class: `run-delta ${deltaTone(delta, key)}`,
                  text: formatDelta(delta),
                  title: "Δ vs parent",
                }),
              ),
        ];
      }),
    );
    const hyperCount = Object.keys(hyper).length;
    const hyperChanged = HYPERPARAMETERS.filter((key) => changed.has(key)).length;
    const runConfig = el(
      "div",
      { class: "nested-card", "data-card": "Run config" },
      el("h3", { class: "eyebrow", text: "Run config" }),
      el("div", { class: "stack" }, parameterSearch, parameterValues),
    );
    anatomy.replaceChildren(
      card(
        "Hyperparameters",
        hyperValues,
        el("span", {
          class: "muted",
          text: hyperCount
            ? `${hyperCount}${parent ? ` · ${hyperChanged} changed` : ""}`
            : "",
        }),
      ),
      card(
        "Summary metrics",
        el("div", { class: "stack" }, kv(run.summary ? summary : {}), runConfig),
        el("span", { class: "muted", text: parent ? "Δ vs parent" : "final values" }),
      ),
      card(
        "Source",
        kv({
          commit: git.git_commit,
          branch: git.git_branch,
          "working tree": git.git_commit ? (git.is_dirty ? "dirty" : "clean") : null,
          entrypoint,
        }),
        gitAside,
      ),
      card("Environment", environment),
    );
    const versionCount = (run.artifacts || []).length;
    outputs.replaceChildren(
      card(
        "Artifacts",
        artifacts,
        el("span", {
          class: "muted",
          text: versionCount ? `${versionCount} version${versionCount === 1 ? "" : "s"}` : "",
        }),
      ),
      mediaCard(media),
    );
    setPollStatus(
      pollStatus,
      run.status === "running" ? "Live · refreshes every 5 s" : "Up to date",
    );
  }
  const fullButton = button("Open full window ⤢", () => code?.openFull());
  app.replaceChildren(
    el(
      "div",
      { class: "detail stack" },
      el("a", { href: "#/", text: "← All runs" }),
      title,
      strip,
      el("section", { class: "card run-facts" }, stats, facts),
      el(
        "div",
        { class: "stack" },
        el(
          "div",
          { class: "heading" },
          eyebrow("Metrics"),
          button("Open in full window ⤢", () => panel.openWorkspace(), "secondary"),
        ),
        panel.node,
      ),
      el(
        "div",
        { class: "stack" },
        el("div", { class: "heading section-head" }, eyebrow("Reproducibility"), reproNote),
        anatomy,
      ),
      outputs,
      el(
        "div",
        { class: "stack" },
        el(
          "div",
          { class: "heading section-head" },
          eyebrow("Code snapshot"),
          snapshotNote,
          fullButton,
        ),
        codeContainer,
      ),
      pollError,
      pollStatus,
    ),
  );
  drawFacts();
  cleanup = () => {
    clearTimeout(timer);
    panel.destroy();
    code?.destroy();
  };
  const codePromise = createCodeViewer({
    run,
    runs: summaries,
    signal,
    el,
    get,
    button,
    focus,
    objectURL,
    parentId: parentId(),
    onBaseline: (baseline, entry) => {
      if (epoch !== version) return;
      if (entry !== entrypoint) {
        entrypoint = entry;
        drawFacts();
      }
      // The parent is the preferred baseline; an earlier run stands in only
      // when the parent has no snapshot, and the label says which it is.
      gitAside.replaceChildren(
        baseline
          ? button(
              baseline.run_id === parentId()
                ? "View diff vs parent"
                : `View diff vs ${baseline.name || shortRunId(baseline.run_id)}`,
              () => code?.openFull(),
            )
          : el("span", { text: parentId() ? "parent has no snapshot" : "root run · no parent" }),
      );
    },
  })
    .then((viewer) => {
      if (epoch !== version) {
        viewer?.destroy();
        return;
      }
      code = viewer;
      if (code) codeContainer.replaceChildren(code.node);
    })
    .catch((error) => {
      if (epoch === version) showError(codeContainer, error);
    });
  async function refresh() {
    try {
      const [next, index, nextMedia] = await Promise.all([
        get(apiRun(id), signal),
        get("/api/runs", signal),
        get(`${apiRun(id)}/media`, signal),
      ]);
      if (epoch !== version) return;
      media = nextMedia;
      summaries = index;
      siblings = index.filter(
        (other) => (other.project || "") === (next.project || ""),
      );
      run = {
        ...next,
        last_step: index.find((item) => item.run_id === id)?.last_step,
        run_id: id,
        status: index.find((run) => run.run_id === id)?.status || next.status,
      };
      drawFacts();
      await panel.update([run]);
      if (epoch !== version) return;
      pollError.replaceChildren();
      if (run.status === "running") timer = setTimeout(refresh, 5000);
    } catch (error) {
      if (epoch === version) {
        showError(pollError, error);
        timer = setTimeout(refresh, 5000);
      }
    }
  }
  await panel.update();
  if (epoch === version && run.status === "running")
    timer = setTimeout(refresh, 5000);
  await codePromise;
}
function formatBytes(value) {
  let size = Number(value) || 0,
    index = 0;
  const units = ["B", "KB", "MB", "GB"];
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index++;
  }
  return `${size.toLocaleString([], { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
}
// Compare: Δ is always "run − reference", where the reference is the first run.
function percent(delta, base) {
  if (!base) return "";
  const share = Math.abs((delta / base) * 100);
  return `${delta > 0 ? "+" : delta < 0 ? "−" : "±"}${share >= 10 ? Math.round(share) : share.toFixed(1)}%`;
}
function configDiff(runs, onlyDifferences) {
  const all = [...new Set(runs.flatMap((run) => Object.keys(run.config || {})))].sort();
  const differ = new Set(variedKeys(runs));
  const keys = onlyDifferences ? all.filter((key) => differ.has(key)) : all;
  const node = table(
    ["Param", ...runs.map((run) => run.name || run.run_id)],
    keys.map((key) => {
      const row = tableRow([
        key,
        ...runs.map((run) =>
          el("span", {
            class: differ.has(key) ? "diff-value" : "same-value",
            text: fmt(run.config?.[key]),
          }),
        ),
      ]);
      if (differ.has(key)) row.classList.add("is-diff");
      return row;
    }),
  );
  return { node, count: `${differ.size} of ${all.length} params differ` };
}
function summaryDiff(runs) {
  const [reference, ...others] = runs;
  const configKeys = new Set(runs.flatMap((run) => Object.keys(run.config || {})));
  const keys = [...new Set(runs.flatMap((run) => Object.keys(run.summary || {})))].sort();
  const headers = [
    "Metric",
    ...runs.map((run) => run.name || run.run_id),
    ...others.map((run) => (others.length > 1 ? `Δ ${run.name || run.run_id}` : "Δ")),
  ];
  const rows = keys.map((key) => {
    const base = reference.summary?.[key];
    const direction = metricDirection(key) ? (lowerBetter(key) ? "↓" : "↑") : "";
    return tableRow([
      el(
        "span",
        { class: "metric-name" },
        key,
        direction ? el("span", { class: "metric-dir", "aria-hidden": "true", text: ` ${direction}` }) : null,
      ),
      ...runs.map((run) => el("span", { class: "mono", text: fmt(run.summary?.[key]) })),
      ...others.map((run) => {
        const value = run.summary?.[key];
        if (!isNumber(value) || !isNumber(base))
          return el("span", { class: "run-delta", text: "—" });
        const delta = value - base;
        const tone = configKeys.has(key) ? "" : deltaTone(delta, key);
        return el("span", {
          class: `run-delta ${tone}`,
          text: delta && base ? `${formatDelta(delta)} (${percent(delta, base)})` : formatDelta(delta),
        });
      }),
    ]);
  });
  return table(headers, rows);
}
// One-line, data-only summary of a two-run comparison: what changed in the
// config and the summary metric that moved most. No cause is claimed.
function comparisonCallout(runs) {
  if (runs.length !== 2) return null;
  const [reference, other] = runs;
  const configKeys = new Set(runs.flatMap((run) => Object.keys(run.config || {})));
  const moves = Object.keys(reference.summary || {})
    .filter(
      (key) =>
        !configKeys.has(key) &&
        isNumber(reference.summary[key]) &&
        isNumber(other.summary?.[key]),
    )
    .map((key) => {
      const a = reference.summary[key],
        b = other.summary[key];
      return { key, a, b, delta: b - a, share: a ? Math.abs((b - a) / a) : Math.abs(b - a) };
    });
  if (!moves.length) return null;
  const top = moves.reduce((best, move) => (move.share > best.share ? move : best));
  const changes = changedKeys(other, reference)
    .slice(0, 3)
    .map((key) => `${key} ${fmt(reference.config?.[key])} → ${fmt(other.config?.[key])}`);
  const unchanged = moves.filter((move) => move.delta === 0).map((move) => move.key);
  const tone = top.delta === 0 ? "note" : deltaTone(top.delta, top.key) === "ok" ? "ok" : "warn";
  const parts = [
    changes.length ? changes.join(", ") : "no param changes",
    top.delta === 0
      ? `${top.key} unchanged`
      : `${top.key} ${fmt(top.a)} → ${fmt(top.b)} (${percent(top.delta, top.a) || formatDelta(top.delta)}, ${tone === "ok" ? "better" : "worse"})`,
    unchanged.length && top.delta !== 0 ? `${unchanged.slice(0, 2).join(", ")} unchanged` : null,
  ].filter(Boolean);
  return el("p", { class: `callout callout-${tone}`, role: "note", text: parts.join(" · ") });
}
async function comparePage(ids, version) {
  const signal = controller.signal;
  const loadRuns = async () => {
    const [index, details] = await Promise.all([
      get("/api/runs", signal),
      Promise.all(
        ids.map(async (id) => ({
          ...(await get(apiRun(id), signal)),
          run_id: id,
        })),
      ),
    ]);
    return details.map((run) => ({
      ...run,
      last_step: index.find((item) => item.run_id === run.run_id)?.last_step,
      status:
        index.find((item) => item.run_id === run.run_id)?.status || run.status,
    }));
  };
  let runs = await loadRuns();
  if (epoch !== version) return;
  const projects = [...new Set(runs.map((run) => run.project || "No project"))];
  const overlay = focus(
    `Compare ${runs.length} runs`,
    runs.length > 1
      ? `${projects.join(", ")} · reference = ${runs[0].name || runs[0].run_id} · Δ = run − reference`
      : `${runs.length} run selected`,
    "compare-focus",
    () => {
      if (epoch === version) location.hash = "#/";
    },
  );
  app.replaceChildren();
  let timer, codeDiff;
  const panel = metricsPanel(runs, true, signal);
  cleanup = () => {
    clearTimeout(timer);
    panel.destroy();
    codeDiff?.destroy();
    overlay.close();
  };
  if (runs.length < 2) {
    overlay.body.append(
      el("p", {
        class: "empty",
        text: "Select two or more runs from Runs to compare.",
      }),
    );
    return;
  }
  // Same palette as the overlaid curves: reference --s5, then --s1…--s4.
  const colors = Object.fromEntries(
    runs.map((run, index) => [
      run.run_id,
      index === 0 ? "var(--s5)" : `var(--s${((index - 1) % 4) + 1})`,
    ]),
  );
  codeDiff = createCodeDiff({ runs, colors, signal, el, get, button });
  const parentOfRun = (run) => run.parent_run_id || run.lineage?.parent_run_id;
  const selected = el(
    "div",
    { class: "compare-legend", "data-testid": "selected-runs" },
    ...runs.map((run, index) =>
      el(
        "span",
        { class: `legend-chip${index === 0 ? " is-reference" : ""}` },
        el("span", {
          class: "legend-swatch",
          "aria-hidden": "true",
          style: `--swatch:${colors[run.run_id]}`,
        }),
        el("span", {
          class: "legend-name",
          title: run.name || run.run_id,
          text: run.name || run.run_id,
        }),
        el("span", {
          class: "legend-role",
          text: `${index === 0 ? "reference" : parentOfRun(run) === runs[0].run_id ? "child" : "run"} · ${shortRunId(run.run_id)}`,
        }),
      ),
    ),
  );
  const onlyDifferences = el("input", {
    type: "checkbox",
    checked: true,
    "aria-label": "Only differences",
  });
  const diffCount = el("span", { class: "muted" }),
    configBody = el("div"),
    summaryBody = el("div"),
    callout = el("div"),
    pollError = el("div"),
    pollStatus = pollStatusNode();
  onlyDifferences.onchange = () => draw();
  function draw() {
    const diff = configDiff(runs, onlyDifferences.checked);
    diffCount.textContent = diff.count;
    configBody.replaceChildren(diff.node);
    summaryBody.replaceChildren(summaryDiff(runs));
    const note = comparisonCallout(runs);
    callout.replaceChildren(...(note ? [note] : []));
    setPollStatus(
      pollStatus,
      runs.some((run) => run.status === "running")
        ? "Live · refreshes every 5 s"
        : "Up to date",
    );
  }
  overlay.body.append(
    el(
      "div",
      { class: "focus-content stack" },
      selected,
      el(
        "div",
        { class: "compare-grid" },
        el(
          "section",
          { class: "card anatomy-card compare-card" },
          el(
            "header",
            {},
            el("h2", { class: "eyebrow", text: "Config diff" }),
            diffCount,
            el("label", { class: "inline only-diff" }, onlyDifferences, "Only differences"),
          ),
          configBody,
        ),
        el(
          "section",
          { class: "card anatomy-card compare-card" },
          el(
            "header",
            {},
            el("h2", { class: "eyebrow", text: "Summary metrics" }),
            el("span", { class: "muted", text: "final values" }),
          ),
          summaryBody,
          callout,
        ),
      ),
      el(
        "section",
        { class: "stack" },
        el(
          "div",
          { class: "heading section-head" },
          el("h2", { class: "eyebrow", text: "Overlaid curves" }),
          el("span", { class: "muted", text: "reference dashed" }),
        ),
        panel.node,
      ),
      codeDiff.node,
      pollError,
      pollStatus,
    ),
  );
  draw();
  async function refresh() {
    try {
      const next = await loadRuns();
      if (epoch !== version) return;
      runs = next;
      codeDiff.update(runs);
      draw();
      await panel.update(runs);
      if (epoch !== version) return;
      pollError.replaceChildren();
      if (runs.some((run) => run.status === "running"))
        timer = setTimeout(refresh, 5000);
    } catch (error) {
      if (epoch === version) {
        showError(pollError, error);
        timer = setTimeout(refresh, 5000);
      }
    }
  }
  await panel.update();
  if (epoch === version && runs.some((run) => run.status === "running"))
    timer = setTimeout(refresh, 5000);
}
async function route() {
  const version = ++epoch;
  controller?.abort();
  cleanup();
  cleanup = () => {};
  document.querySelector("#lightbox").close();
  controller = new AbortController();
  app.replaceChildren(loading("Loading runs…"));
  try {
    const hash = location.hash.slice(1) || "/";
    if (hash.startsWith("/run/"))
      await runPage(decodeURIComponent(hash.slice(5)), version);
    else if (hash.startsWith("/compare")) {
      const query = new URLSearchParams(hash.slice(hash.indexOf("?") + 1));
      const parameters = query.getAll("runs");
      // Generated links mark repeated parameters, including a single comma ID.
      // Continue accepting historical links with one comma-separated parameter.
      const ids = [
        ...new Set(
          (parameters.length > 1 || query.has("selection")
            ? parameters
            : (parameters[0] || "").split(",")
          ).filter(Boolean),
        ),
      ];
      await comparePage(ids, version);
    } else await home(version);
  } catch (error) {
    if (version === epoch) showError(app, error);
  }
}
window.addEventListener("hashchange", route);
route();
