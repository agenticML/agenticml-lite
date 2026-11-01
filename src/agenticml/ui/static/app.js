/* AgenticML local dashboard. Store strings enter the DOM only as text. */
import { MetricChart, emaSmooth, colorForId, formatNumber } from "./charts.js";
import { createCodeViewer } from "./code.js";
import { createCodeDiff } from "./codediff.js";
import { createNotepad, createNotebook } from "./notes.js";

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
// Runs filter. Words match name, id and tags; "tag:x" and "status:x" (with
// "failed" for crashed) narrow by tag and status; "key>n", "key<n", "key>=n",
// "key<=n" and "key=v" test config, then summary, values. Every token must hold.
const FILTER_COMPARISON = /^([^<>=:]+)(>=|<=|>|<|=)(.+)$/;
function runMatches(run, query) {
  const text = `${run.name} ${run.run_id} ${(run.tags || []).join(" ")}`.toLowerCase();
  return query
    .split(/\s+/)
    .filter(Boolean)
    .every((token) => {
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
// Free-text part of a filter, for the notes notebook (no field operators).
const filterWords = (query) =>
  query
    .split(/\s+/)
    .filter((token) => token && !/^(tag|status):/.test(token) && !FILTER_COMPARISON.test(token))
    .join(" ");
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
    if (topmost !== host || document.querySelector("#lightbox[open]")) return;
    if (event.key === "Escape") {
      event.stopImmediatePropagation();
      close();
    } else if (event.key === "Tab") {
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
const LINEAGE_W = 236;
const LINEAGE_H = 64;
const LINEAGE_GAP_X = 32;
const LINEAGE_GAP_Y = 48;
const LINEAGE_PAD = 16;
let lineageMarker = 0;
let lineageLane = 0;
function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child != null));
  return node;
}
const cssEscape = (value) =>
  window.CSS?.escape ? CSS.escape(String(value)) : String(value).replace(/"/g, '\\"');
const shorten = (value, max) => {
  const text = value || "";
  return text.length > max ? `${text.slice(0, Math.max(1, max - 1))}…` : text;
};
const RUN_ID_SUFFIX = /^(\d{8}T\d{6})-([0-9a-fA-F]{8})$/;
function shortRunId(id) {
  const text = String(id || "");
  const matched = RUN_ID_SUFFIX.exec(text);
  return matched ? matched[2] : text.slice(0, 8);
}
const lineageTime = (value) => {
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : Number.POSITIVE_INFINITY;
};
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
// Generations run top to bottom: layer is the longest path from a root.
// A barycenter sweep pulls related nodes into the same column; ties stay
// in created_at order.
function layoutLineage(nodes, edges, nodeHeight = LINEAGE_H) {
  const byId = new Map(nodes.map((node) => [node.run_id, node]));
  const parents = new Map();
  const children = new Map();
  for (const edge of edges) {
    if (!byId.has(edge.from) || !byId.has(edge.to) || edge.from === edge.to)
      continue;
    if (!parents.has(edge.to)) parents.set(edge.to, []);
    parents.get(edge.to).push(edge.from);
    if (!children.has(edge.from)) children.set(edge.from, []);
    children.get(edge.from).push(edge.to);
  }
  const layerOf = new Map();
  const visiting = new Set();
  const layer = (id) => {
    if (layerOf.has(id)) return layerOf.get(id);
    if (visiting.has(id)) return 0;
    visiting.add(id);
    let value = 0;
    for (const parent of parents.get(id) || [])
      value = Math.max(value, layer(parent) + 1);
    visiting.delete(id);
    layerOf.set(id, value);
    return value;
  };
  for (const node of nodes) layer(node.run_id);
  const depth = [...layerOf.values()].reduce((max, value) => Math.max(max, value), 0);
  const layers = Array.from({ length: depth + 1 }, () => []);
  const ordered = [...nodes].sort(
    (a, b) =>
      lineageTime(a.created_at) - lineageTime(b.created_at) ||
      String(a.run_id).localeCompare(String(b.run_id)),
  );
  for (const node of ordered) layers[layerOf.get(node.run_id) || 0].push(node);
  const index = new Map();
  const reindex = () => {
    index.clear();
    layers.forEach((row) =>
      row.forEach((node, position) => index.set(node.run_id, position)),
    );
  };
  const barycenter = (row, neighborOf) => {
    const ranked = row.map((node, position) => {
      const neighbors = (neighborOf(node.run_id) || []).filter((id) =>
        index.has(id),
      );
      const center = neighbors.length
        ? neighbors.reduce((sum, id) => sum + index.get(id), 0) /
          neighbors.length
        : position;
      return { node, center, position };
    });
    ranked.sort(
      (a, b) =>
        a.center - b.center ||
        lineageTime(a.node.created_at) - lineageTime(b.node.created_at) ||
        String(a.node.run_id).localeCompare(String(b.node.run_id)) ||
        a.position - b.position,
    );
    return ranked.map((item) => item.node);
  };
  reindex();
  for (let pass = 0; pass < 2; pass++) {
    for (let i = 1; i < layers.length; i++) {
      layers[i] = barycenter(layers[i], (id) => parents.get(id));
      reindex();
    }
    for (let i = layers.length - 2; i >= 0; i--) {
      layers[i] = barycenter(layers[i], (id) => children.get(id));
      reindex();
    }
  }
  const pos = new Map();
  let breadth = 0;
  layers.forEach((row, generation) => {
    row.forEach((node, index) => {
      pos.set(node.run_id, {
        x: LINEAGE_PAD + index * (LINEAGE_W + LINEAGE_GAP_X),
        y: LINEAGE_PAD + generation * (nodeHeight + LINEAGE_GAP_Y),
        generation,
        index,
      });
    });
    breadth = Math.max(breadth, row.length);
  });
  return {
    pos,
    width:
      LINEAGE_PAD * 2 +
      Math.max(breadth, 1) * LINEAGE_W +
      Math.max(breadth - 1, 0) * LINEAGE_GAP_X,
    height:
      LINEAGE_PAD * 2 +
      Math.max(layers.length, 1) * nodeHeight +
      Math.max(layers.length - 1, 0) * LINEAGE_GAP_Y,
  };
}
const lineageTask = (node) => typeof node.task === "string" ? node.task : "";
function lineageBadges(node, byId) {
  const badges = [];
  const parent = byId.get(node.parent_run_id);
  if (node.base_dirty) badges.push(["dirty", "lineage-dirty", "Base tree was dirty"]);
  if (node.same_tree_as_parent)
    badges.push(["code unchanged", "lineage-same", "Same tree as parent"]);
  if (node.error) badges.push(["error", "lineage-error", String(node.error)]);
  if (node.cross_task && parent)
    badges.push([
      `from ${lineageTask(parent) || "No task"}`, "lineage-cross-task",
      `from ${lineageTask(parent) || "No task"}: ${parent.name || parent.run_id} (${parent.run_id})`,
    ]);
  if (node.arch_changed)
    badges.push([
      "arch changed", "lineage-arch-changed",
      `${parent?.arch?.model_class || "—"} → ${node.arch?.model_class || "—"}`,
    ]);
  return badges;
}
function renderLineageLanes(data, options) {
  const lanes = new Map();
  const byId = new Map(data.nodes.map((node) => [node.run_id, node]));
  for (const node of data.nodes) {
    const task = lineageTask(node);
    if (!lanes.has(task)) lanes.set(task, []);
    lanes.get(task).push(node);
  }
  const graph = (nodes) => {
    const ids = new Set(nodes.map((node) => node.run_id));
    return renderLineageSvg({
      nodes,
      edges: data.edges.filter((edge) =>
        edge && !edge.cross_task && ids.has(edge.from) && ids.has(edge.to)),
    }, { ...options, byId });
  };
  // Preserve the original DOM and layout for the single unassigned stream.
  if (lanes.size === 1 && lanes.has("")) return [graph(data.nodes)];
  const timestamps = new Map(data.nodes.map((node) => {
    const time = Date.parse(node.created_at);
    return [node.run_id, Number.isFinite(time) ? time : -Infinity];
  }));
  const orderedLanes = [...lanes].map(([task, nodes]) => {
    nodes.sort((a, b) => timestamps.get(b.run_id) - timestamps.get(a.run_id) ||
      String(a.run_id).localeCompare(String(b.run_id)));
    const recent = nodes.reduce((max, node) => Math.max(max, timestamps.get(node.run_id)), -Infinity);
    return [task, nodes, recent];
  });
  return orderedLanes.sort(([a, , aa], [b, , bb]) =>
    bb - aa || a.localeCompare(b),
  ).map(([task, nodes]) => {
    const source = nodes.find((node) => node.task_source)?.task_source;
    const hint = source === "entry" ? `from ${task}/` :
      source === "marker" ? "agenticml.toml" :
      source === "parent" ? "from parent run" : source || "";
    const headingId = `lineage-lane-title-${++lineageLane}`;
    return el("section", { class: "lineage-lane", "data-task": task, "aria-labelledby": headingId },
      el("header", { class: "lineage-lane-head" },
        el("h3", { id: headingId, class: "lineage-lane-title", text: task || "No task" }),
        hint ? el("span", { class: "muted lineage-lane-source", text: hint }) : null,
      ),
      el("div", { class: "lineage-lane-scroll" }, graph(nodes)),
    );
  });
}
function renderLineageSvg(data, options) {
  const nodes = data.nodes || [];
  const edges = (data.edges || []).filter(
    (edge) => edge && edge.from && edge.to && edge.from !== edge.to,
  );
  const badgesById = new Map(nodes.map((node) => {
    let x = 28, row = 0;
    const badges = lineageBadges(node, options.byId).map(([label, className, tip]) => {
      const text = shorten(label, 26);
      const size = text.length * 5.6 + 10;
      if (x > 28 && x + size > LINEAGE_W - 6) { x = 28; row++; }
      const badge = { text, className, tip, x, row, label };
      x += size;
      return badge;
    });
    return [node.run_id, badges];
  }));
  const rows = [...badgesById.values()].reduce((max, badges) =>
    badges.reduce((max, badge) => Math.max(max, badge.row + 1), max), 1);
  const nodeHeight = LINEAGE_H + (rows - 1) * 14;
  const { pos, width, height } = layoutLineage(nodes, edges, nodeHeight);
  const markerId = `lineage-arrow-${++lineageMarker}`;
  const filtering = options.filtering;
  const matching = options.matching || new Set();
  const dimmed = (id) => filtering && !matching.has(id);
  const paths = edges.map((edge) => {
    const from = pos.get(edge.from);
    const to = pos.get(edge.to);
    if (!from || !to) return null;
    const x1 = from.x + LINEAGE_W / 2;
    const y1 = from.y + nodeHeight;
    const x2 = to.x + LINEAGE_W / 2;
    const y2 = to.y;
    const bend = Math.max(24, (y2 - y1) / 2);
    const faded = dimmed(edge.from) || dimmed(edge.to);
    return svgEl("path", {
      class: `lineage-edge${edge.inferred ? " is-inferred" : ""}${faded ? " is-dim" : ""}`,
      "data-from": edge.from,
      "data-to": edge.to,
      d: `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`,
      "marker-end": `url(#${markerId})`,
    });
  });
  const glyphs = nodes.map((node) => {
    const at = pos.get(node.run_id) || {
      x: LINEAGE_PAD,
      y: LINEAGE_PAD,
      generation: 0,
      index: 0,
    };
    const name = node.name || node.run_id || "run";
    const status = node.status || "unknown";
    const tone = ["running", "finished", "crashed", "stale"].includes(status)
      ? status
      : "unknown";
    const badges = badgesById.get(node.run_id);
    const badgeNodes = badges.map(({ text, className, tip, x, row }) =>
      svgEl(
        "text",
        { class: className, x: at.x + x, y: at.y + 54 + row * 14, text },
        svgEl("title", { text: tip }),
      ),
    );
    const commit = node.commit ? String(node.commit).slice(0, 7) : "—";
    const picked = options.selected?.has(node.run_id);
    const runId = node.run_id || "";
    return svgEl(
      "g",
      {
        class: `lineage-node${dimmed(node.run_id) ? " is-dim" : ""}`,
        role: "button",
        tabindex: "0",
        "data-run-id": node.run_id,
        "data-generation": at.generation ?? 0,
        "data-sibling": at.index ?? 0,
        "data-picked": picked ? "true" : "false",
        "aria-pressed": picked ? "true" : "false",
        "aria-label": [name, status, runId, ...badges.map((badge) => badge.label)].join(", "),
      },
      svgEl("title", { text: `${name}\n${runId}` }),
      svgEl("rect", {
        class: "lineage-shape",
        x: at.x,
        y: at.y,
        width: LINEAGE_W,
        height: nodeHeight,
        rx: "8",
      }),
      svgEl("circle", {
        class: `lineage-dot ${tone}`,
        cx: at.x + 16,
        cy: at.y + 20,
        r: "4",
      }),
      svgEl(
        "text",
        { class: "lineage-name", x: at.x + 28, y: at.y + 24, text: shorten(name, 20) },
        svgEl("title", { text: name }),
      ),
      svgEl("text", {
        class: "lineage-role",
        x: at.x + LINEAGE_W - 12,
        y: at.y + 24,
        "text-anchor": "end",
        text: node.parent_run_id ? "child" : "root",
      }),
      svgEl(
        "text",
        {
          class: "lineage-meta",
          x: at.x + 28,
          y: at.y + 40,
          text: `run ${shortRunId(runId)} · snapshot ${commit}`,
        },
        svgEl("title", { text: runId }),
      ),
      ...badgeNodes,
    );
  });
  return svgEl(
    "svg",
    {
      class: "lineage-graph",
      width: width,
      height: height,
      viewBox: `0 0 ${width} ${height}`,
      role: "group",
      "aria-label": "Run lineage",
    },
    svgEl(
      "defs",
      {},
      svgEl(
        "marker",
        {
          id: markerId,
          viewBox: "0 0 8 8",
          refX: "8",
          refY: "4",
          markerWidth: "7",
          markerHeight: "7",
          orient: "auto",
        },
        svgEl("path", {
          class: "lineage-arrow",
          d: "M0,0 L8,4 L0,8 Z",
          style: "fill: var(--text-3)",
        }),
      ),
    ),
    ...paths,
    ...glyphs,
  );
}
async function home(version) {
  const selected = new Set();
  const search = el("input", {
    type: "search",
    class: "field",
    placeholder: "lr>0.1   tag:baseline   status:failed",
    "aria-label": "Filter runs",
  });
  const tabs = el("div", {
    class: "tabs",
    role: "tablist",
    "aria-label": "Projects",
  });
  const notebook = createNotebook({ el, button, get, routeRun });
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
          text: "Grouped by project · sorted by start time",
        }),
      ),
      el("div", { class: "selection-slot" }, selection),
      content,
      notebook.node,
      status,
    ),
  );
  let runs = [],
    project = null,
    timer;
  const EMPTY_SELECTION =
    "Select two or more runs to overlay their curves and diff their configs.";
  const selectionCount = el("span", { text: EMPTY_SELECTION });
  const selectionChips = el("span", { class: "selection-chips", hidden: true });
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
  selection.append(selectionCount, selectionChips, selectionHint, selectionActions);
  function updateSelection() {
    selection.classList.toggle("is-empty", !selected.size);
    selectionActions.hidden = !selected.size;
    selectionChips.hidden = !selected.size;
    selectionHint.hidden = selected.size < 2;
    selectionCount.textContent = selected.size
      ? `${selected.size} run${selected.size === 1 ? "" : "s"} selected`
      : EMPTY_SELECTION;
    const names = runs
      .filter((run) => selected.has(run.run_id))
      .map((run) => run.name || run.run_id);
    if (selectionChips.dataset.names !== JSON.stringify(names)) {
      selectionChips.dataset.names = JSON.stringify(names);
      selectionChips.replaceChildren(...names.map((name) => chip(name)));
    }
    compareButton.disabled = selected.size < 2;
    for (const row of content.querySelectorAll(".run-row")) {
      const picked = selected.has(row.dataset.runId);
      row.dataset.picked = picked;
      row.querySelector(".check").checked = picked;
    }
    for (const node of content.querySelectorAll(".lineage-node")) {
      const picked = selected.has(node.dataset.runId);
      node.dataset.picked = picked;
      node.setAttribute("aria-pressed", picked ? "true" : "false");
    }
  }
  // "Changed vs parent" and "Δ vs parent" read the lineage parent. Until a
  // project's lineage answers, both cells stay "—" rather than guessing.
  function parentFacts(run, metricKey, data, keys) {
    const unknown = { changed: "—", delta: "—", tone: "" };
    if (!data?.nodes) return unknown;
    data.byId ||= new Map(data.nodes.map((node) => [node.run_id, node]));
    const node = data.byId.get(run.run_id);
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
  // Lineage lands after the table; patch the existing rows in place.
  function annotateRows(card, data) {
    const metricKey = card.dataset.headline;
    const keys = variedKeys(
      runs.filter((run) => (run.project || "") === (card.dataset.project || "")),
    );
    for (const row of card.querySelectorAll(".run-row")) {
      const run = runs.find((other) => other.run_id === row.dataset.runId);
      if (!run) continue;
      const facts = parentFacts(run, metricKey, data, keys);
      const changed = row.querySelector(".run-varied");
      if (changed.textContent !== facts.changed) {
        changed.textContent = facts.changed;
        changed.title = facts.changed;
      }
      const delta = row.querySelector(".run-delta");
      if (delta.textContent !== facts.delta) delta.textContent = facts.delta;
      delta.className = `run-number run-delta ${facts.tone}`;
    }
  }
  const lineageCache = new Map();
  const lineageRetry = new Map();
  let lineageToken = 0;
  let detailToken = 0;
  function clearLineageRetry(key) {
    const pending = lineageRetry.get(key);
    if (!pending) return;
    clearTimeout(pending.timer);
    lineageRetry.delete(key);
  }
  // 1s, 2s, 4s… capped at 30s. Independent of the runs poll, so an idle
  // workspace still retries a failed /api/lineage fetch.
  function scheduleLineageRetry(key) {
    if (epoch !== version) return;
    const pending = lineageRetry.get(key);
    const delay = Math.min(
      pending ? pending.delay * 2 : LINEAGE_RETRY_MS,
      LINEAGE_RETRY_MAX_MS,
    );
    if (pending) clearTimeout(pending.timer);
    const timer = setTimeout(() => {
      if (epoch !== version) return;
      const entry = lineageCache.get(key);
      const card = entry?.card;
      const shown = runs.some((run) => (run.project || "") === key);
      if (!entry || !shown || !card?.isConnected) {
        if (!shown) lineageCache.delete(key);
        clearLineageRetry(key);
        return;
      }
      // A newer fetch owns this project. Keep the delay for its failure.
      if (!entry.error || entry.pending) return;
      const siblings = runs.filter((run) => (run.project || "") === key);
      const signature = lineageSignature(siblings);
      card.dataset.signature = signature;
      ensureLineage(
        key,
        signature,
        card,
        siblings.some((run) => run.status === "running"),
      );
    }, delay);
    lineageRetry.set(key, { timer, delay });
  }
  let pendingFocus = { runId: "", project: "" };
  function projectHead(key) {
    return el(
      "header",
      { class: "project-head inline" },
      el("h2", { text: key || "No project" }),
      el("span", { class: "project-meta" }),
      !key
        ? el("span", {
            class: "project-note",
            text: "no project — pass project= or run inside a repo",
          })
        : null,
      el("span", { class: "project-metric" }),
    );
  }
  // A project's entrypoint is what its newest snapshot recorded as launched.
  const entrypoints = new Map();
  function projectMeta(card, siblings) {
    const key = card.dataset.project || "";
    const entry = entrypoints.get(key);
    const text = [
      `${siblings.length} run${siblings.length === 1 ? "" : "s"}`,
      entry ? `entrypoint ${entry}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    const meta = card.querySelector(".project-meta");
    if (meta.textContent !== text) meta.textContent = text;
    if (entrypoints.has(key) || !siblings[0]) return;
    entrypoints.set(key, null);
    get(`${apiRun(siblings[0].run_id)}/code`)
      .then((files) => {
        const path = files.find?.((file) => file.source === "entrypoint")?.path;
        if (!path || epoch !== version) return;
        entrypoints.set(key, path);
        if (card.isConnected) projectMeta(card, siblings);
      })
      .catch(() => {});
  }
  function renderDetail(host, node, detail) {
    const parentId = node.parent_run_id;
    const parentRun = runs.find((run) => run.run_id === parentId);
    const parent = parentId
      ? el("a", {
          href: routeRun(parentId),
          text: parentRun?.name || parentId,
        })
      : el("span", { text: "—" });
    const inferred =
      node.parent_inference && node.parent_inference !== "explicit"
        ? el("span", { class: "muted", text: node.parent_inference })
        : null;
    const base = [
      node.base_commit ? String(node.base_commit).slice(0, 7) : "",
      node.base_branch || "",
    ]
      .filter(Boolean)
      .join(" · ");
    const files = Array.isArray(detail.files) ? detail.files : [];
    const shown = files.slice(0, 8);
    const extra = files.length - shown.length;
    const checkout =
      detail.checkout || `agenticml lineage checkout ${node.run_id}`;
    const copy = button("Copy", () => {
      try {
        const pending = navigator.clipboard.writeText(checkout);
        Promise.resolve(pending).then(
          () => {
            copy.textContent = "Copied";
          },
          () => {
            copy.textContent = "Copy failed";
          },
        );
      } catch {
        copy.textContent = "Copy failed";
      }
    });
    host.replaceChildren(
      ...[
      el(
        "div",
        { class: "lineage-facts" },
        el("div", {}, el("div", { class: "eyebrow", text: "Parent" }), el("div", { class: "inline" }, parent, inferred)),
        el(
          "div",
          {},
          el("div", { class: "eyebrow", text: "Commit" }),
          el("span", {
            class: "mono",
            text: detail.commit || node.commit || "—",
            title: detail.commit || node.commit || "",
          }),
        ),
        el(
          "div",
          {},
          el("div", { class: "eyebrow", text: "Base" }),
          el("span", {
            class: "mono",
            text: `${base || "—"}${node.base_dirty ? " · dirty" : ""}`,
          }),
        ),
        node.arch ? el("div", {},
          el("div", { class: "eyebrow", text: "Model class" }),
          el("span", { class: "mono", text: node.arch.model_class || "—" }),
        ) : null,
        node.arch?.model_type ? el("div", {},
          el("div", { class: "eyebrow", text: "Model type" }),
          el("span", { class: "mono", text: node.arch.model_type }),
        ) : null,
      ),
      detail.error ? el("p", { class: "error", text: detail.error }) : null,
      el("div", { class: "eyebrow", text: "Files changed vs parent" }),
      shown.length
        ? el(
            "div",
            { class: "lineage-files" },
            ...shown.map((file) =>
              el(
                "div",
                { class: "lineage-file" },
                el("span", {
                  class: "path",
                  text: file.path || "—",
                  title: file.path || "",
                }),
                el("span", {
                  class: "added",
                  text: file.added == null ? "—" : `+${file.added}`,
                }),
                el("span", {
                  class: "deleted",
                  text: file.deleted == null ? "—" : `-${file.deleted}`,
                }),
              ),
            ),
          )
        : el("p", { class: "muted", text: detail.error ? "—" : "No file changes." }),
      extra > 0
        ? el("p", {
            class: "muted",
            text: `and ${extra} more${detail.truncated ? " (list truncated)" : ""}`,
          })
        : detail.truncated
          ? el("p", { class: "muted", text: "list truncated" })
          : null,
      el(
        "div",
        { class: "lineage-checkout" },
        el("code", { text: checkout }),
        copy,
      ),
    ].filter((child) => child != null));
  }
  function lineageDetailPayload(node) {
    const parentId = node.parent_run_id || "";
    const parent = runs.find((run) => run.run_id === parentId);
    return JSON.stringify([
      node.run_id || "",
      parentId,
      parent?.name || "",
      node.parent_inference || "",
      node.commit || "",
      node.base_commit || "",
      node.base_branch || "",
      Boolean(node.base_dirty),
      node.task || "",
      node.arch || null,
    ]);
  }
  function detailResponseCurrent(card, token, payload) {
    return (
      epoch === version &&
      card.isConnected &&
      card.dataset.detailToken === token &&
      card.dataset.detailWanted === payload
    );
  }
  function showLineageDetail(card, node) {
    const token = String(++detailToken);
    const payload = lineageDetailPayload(node);
    const sameNode = card.dataset.detailFor === node.run_id;
    card.dataset.detailToken = token;
    card.dataset.detailFor = node.run_id;
    card.dataset.focus = node.run_id;
    card.dataset.detailWanted = payload;
    card.dataset.detailPending = payload;
    const host = card.querySelector(".lineage-detail");
    // A refresh of the open strip keeps the previous parent, commit, and
    // dirty flag until the new payload arrives.
    if (!sameNode)
      host.replaceChildren(loading("Loading changes…"));
    get(`${apiRun(node.run_id)}/lineage`)
      .then((detail) => {
        if (!detailResponseCurrent(card, token, payload)) return;
        card.dataset.detailPayload = payload;
        delete card.dataset.detailPending;
        renderDetail(host, node, detail);
      })
      .catch((error) => {
        if (epoch !== version || error.name === "AbortError") return;
        if (!detailResponseCurrent(card, token, payload)) return;
        delete card.dataset.detailPending;
        // The strip no longer shows this payload. Leaving the rendered
        // marker set makes the next paint treat it as current and skip
        // the refetch, so the error stays up.
        delete card.dataset.detailPayload;
        showError(host, error);
      });
  }
  function toggleLineage(card, node) {
    if (selected.has(node.run_id)) selected.delete(node.run_id);
    else selected.add(node.run_id);
    card.dataset.focus = node.run_id;
    updateSelection();
    showLineageDetail(card, node);
  }
  function lineageNodeFrom(card, event) {
    const target = event.target;
    if (!target || !target.closest) return null;
    const glyph = target.closest(".lineage-node");
    if (!glyph || !card.contains(glyph)) return null;
    const runId = glyph.getAttribute("data-run-id");
    if (!runId) return null;
    const data = lineageCache.get(card.dataset.project || "")?.data;
    return (
      (data?.nodes || []).find((node) => node.run_id === runId) || {
        run_id: runId,
      }
    );
  }
  // Up/Down walk generations (along an edge when there is one). Left/Right
  // walk siblings in the same generation.
  function moveLineageFocus(card, event) {
    const key = event.key;
    const vertical = key === "ArrowUp" || key === "ArrowDown";
    const horizontal = key === "ArrowLeft" || key === "ArrowRight";
    if (!vertical && !horizontal) return false;
    if (event.altKey || event.metaKey || event.ctrlKey || event.shiftKey)
      return false;
    const target = event.target;
    if (!target || !target.closest) return false;
    const glyph = target.closest(".lineage-node");
    if (!glyph || !card.contains(glyph)) return false;
    event.preventDefault();
    const scroll = glyph.closest(".lineage-scroll");
    if (!scroll) return true;
    const graph = glyph.closest(".lineage-graph");
    const nodes = [...graph.querySelectorAll(".lineage-node")];
    const generation = Number(glyph.dataset.generation);
    const sibling = Number(glyph.dataset.sibling);
    const axis = (node, name) =>
      Number(node.querySelector(".lineage-shape")?.getAttribute(name)) || 0;
    let candidates = [];
    if (horizontal) {
      const step = key === "ArrowRight" ? 1 : -1;
      candidates = nodes.filter(
        (node) =>
          Number(node.dataset.generation) === generation &&
          Number(node.dataset.sibling) === sibling + step,
      );
    } else {
      const down = key === "ArrowDown";
      const id = glyph.getAttribute("data-run-id");
      const ids = new Set(
        [...graph.querySelectorAll(".lineage-edge")]
          .filter((edge) =>
            down ? edge.dataset.from === id : edge.dataset.to === id,
          )
          .map((edge) => (down ? edge.dataset.to : edge.dataset.from)),
      );
      candidates = nodes.filter((node) =>
        ids.has(node.getAttribute("data-run-id")),
      );
      if (!candidates.length) {
        const nextGeneration = generation + (down ? 1 : -1);
        candidates = nodes.filter(
          (node) => Number(node.dataset.generation) === nextGeneration,
        );
      }
      // At a lane boundary, continue in the adjacent lane. Tab also reaches
      // every run; within a lane the original generation/sibling rules apply.
      if (!candidates.length) {
        const graphs = [...scroll.querySelectorAll(".lineage-graph")];
        const adjacent = graphs[graphs.indexOf(graph) + (down ? 1 : -1)];
        if (adjacent) {
          const nextNodes = [...adjacent.querySelectorAll(".lineage-node")];
          const generations = nextNodes.map((node) => Number(node.dataset.generation));
          const boundary = down ? Math.min(...generations) : Math.max(...generations);
          candidates = nextNodes.filter((node) => Number(node.dataset.generation) === boundary);
        }
      }
    }
    if (!candidates.length) return true;
    const x = axis(glyph, "x");
    const y = axis(glyph, "y");
    candidates.sort(
      (a, b) =>
        Math.abs(axis(a, "x") - x) - Math.abs(axis(b, "x") - x) ||
        Math.abs(axis(a, "y") - y) - Math.abs(axis(b, "y") - y) ||
        Number(a.dataset.sibling) - Number(b.dataset.sibling) ||
        String(a.dataset.runId).localeCompare(String(b.dataset.runId)),
    );
    candidates[0].focus();
    return true;
  }
  function bindLineageCard(card) {
    if (card.dataset.lineageBound === "1") return;
    card.dataset.lineageBound = "1";
    for (const type of ["mouseover", "mouseout"])
      card.addEventListener(type, (event) => {
        const source = event.target.closest?.(".run-row, .lineage-node");
        if (!source?.isConnected || source.contains(event.relatedTarget)) return;
        for (const peer of card.querySelectorAll(
          `[data-run-id="${cssEscape(source.dataset.runId)}"]`,
        ))
          peer.classList.toggle("is-linked", type === "mouseover");
      });
    card.addEventListener("click", (event) => {
      if (event.detail > 1) return;
      const node = lineageNodeFrom(card, event);
      if (!node) return;
      toggleLineage(card, node);
    });
    card.addEventListener("dblclick", (event) => {
      const node = lineageNodeFrom(card, event);
      if (!node) return;
      event.preventDefault();
      location.hash = routeRun(node.run_id);
    });
    card.addEventListener("keydown", (event) => {
      if (moveLineageFocus(card, event)) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      const node = lineageNodeFrom(card, event);
      if (!node) return;
      event.preventDefault();
      if (event.key === "Enter") location.hash = routeRun(node.run_id);
      else toggleLineage(card, node);
    });
  }
  function restoreLineageHover(card) {
    const source = card.querySelector(".run-row:hover, .lineage-node:hover");
    if (!source) return;
    for (const peer of card.querySelectorAll(
      `[data-run-id="${cssEscape(source.dataset.runId)}"]`,
    ))
      peer.classList.add("is-linked");
  }
  function paintLineage(card, data) {
    const scroll = card.querySelector(".lineage-scroll");
    if (!scroll) return;
    if (!data || !Array.isArray(data.nodes) || !Array.isArray(data.edges)) {
      showError(scroll, new Error("Invalid lineage response"));
      return;
    }
    const active = document.activeElement;
    const focusedNode = active?.closest?.(".lineage-node");
    const typing =
      active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA");
    const inThisGraph = Boolean(focusedNode && scroll.contains(focusedNode));
    const projectKey = card.dataset.project ?? "";
    const blurredHere =
      !typing &&
      !inThisGraph &&
      pendingFocus.runId &&
      pendingFocus.project === projectKey &&
      (!active ||
        active === document.body ||
        active === document.documentElement);
    const restoreId = inThisGraph
      ? focusedNode.getAttribute("data-run-id") || ""
      : blurredHere
        ? pendingFocus.runId
        : "";
    const top = scroll.scrollTop;
    const left = scroll.scrollLeft;
    const laneScrolls = new Map([...scroll.querySelectorAll(".lineage-lane")].map(
      (lane) => [lane.dataset.task, lane.querySelector(".lineage-lane-scroll").scrollLeft],
    ));
    if (!data.nodes.length) {
      scroll.replaceChildren(
        el("p", { class: "muted", text: "No runs to graph." }),
      );
      return;
    }
    const query = search.value.trim().toLowerCase();
    const siblings = runs.filter((run) => (run.project || "") === projectKey);
    const matching = new Set(
      siblings
        .filter((run) => runMatches(run, query))
        .map((run) => run.run_id),
    );
    scroll.replaceChildren(
      ...renderLineageLanes(data, {
        filtering: Boolean(query),
        matching,
        selected,
      }),
    );
    for (const lane of scroll.querySelectorAll(".lineage-lane"))
      lane.querySelector(".lineage-lane-scroll").scrollLeft = laneScrolls.get(lane.dataset.task) || 0;
    scroll.scrollTop = top;
    scroll.scrollLeft = left;
    restoreLineageHover(card);
    if (restoreId && !typing) {
      const live = scroll.querySelector(
        `.lineage-node[data-run-id="${cssEscape(restoreId)}"]`,
      );
      if (live) {
        try {
          live.focus({ preventScroll: true });
        } catch {
          live.focus();
        }
        scroll.scrollTop = top;
        scroll.scrollLeft = left;
      }
    }
    updateSelection();
    const focusId = card.dataset.focus;
    if (!focusId) return;
    const node = data.nodes.find((item) => item.run_id === focusId);
    if (!node) return;
    const payload = lineageDetailPayload(node);
    // Wanted is whatever the most recent paint asked to show. A response
    // may render only while it is still that payload.
    if (payload !== card.dataset.detailWanted) {
      const rendered =
        payload === card.dataset.detailPayload &&
        card.dataset.detailFor === focusId;
      card.dataset.detailWanted = payload;
      if (rendered) {
        card.dataset.detailToken = String(++detailToken);
        delete card.dataset.detailPending;
      } else showLineageDetail(card, node);
      return;
    }
    if (
      card.dataset.detailFor !== focusId ||
      (card.dataset.detailPayload !== payload && card.dataset.detailPending !== payload)
    )
      showLineageDetail(card, node);
  }
  function dimLineage(card, siblings, query) {
    const matching = new Set(
      siblings.filter((run) => runMatches(run, query)).map((run) => run.run_id),
    );
    const filtering = Boolean(query);
    for (const node of card.querySelectorAll(".lineage-node"))
      node.classList.toggle("is-dim", filtering && !matching.has(node.dataset.runId));
    for (const edge of card.querySelectorAll(".lineage-edge")) {
      const faded =
        filtering &&
        (!matching.has(edge.dataset.from) || !matching.has(edge.dataset.to));
      edge.classList.toggle("is-dim", faded);
    }
  }
  function ensureLineage(key, signature, card, running) {
    const now = Date.now();
    const scroll = card.querySelector(".lineage-scroll");
    let entry = lineageCache.get(key);
    const same = entry && entry.signature === signature;
    const due =
      Boolean(running) &&
      same &&
      !entry.pending &&
      now - (entry.fetchedAt || 0) >= LINEAGE_REFRESH_MS;
    if (same && !due && !entry.error) {
      entry.card = card;
      if (entry.data) paintLineage(card, entry.data);
      else if (scroll && !scroll.querySelector(".lineage-graph, [role=status]"))
        scroll.replaceChildren(loading("Loading lineage…"));
      return;
    }
    const token = ++lineageToken;
    const kept = entry?.data || null;
    entry = {
      signature,
      token,
      data: kept,
      error: null,
      pending: true,
      card,
      fetchedAt: now,
    };
    lineageCache.set(key, entry);
    if (scroll && !scroll.querySelector(".lineage-graph")) {
      if (kept) paintLineage(card, kept);
      else if (!scroll.querySelector("[role=status]"))
        scroll.replaceChildren(loading("Loading lineage…"));
    }
    get(`/api/lineage?${new URLSearchParams({ project: key })}`)
      .then((data) => {
        if (epoch !== version) return;
        const current = lineageCache.get(key);
        if (!current || current.token !== token) return;
        current.data = data;
        current.pending = false;
        current.error = null;
        clearLineageRetry(key);
        const target = current.card;
        if (target?.isConnected && target.dataset.signature === signature) {
          paintLineage(target, data);
          annotateRows(target, data);
        }
      })
      .catch((error) => {
        if (epoch !== version || error.name === "AbortError") return;
        const current = lineageCache.get(key);
        if (!current || current.token !== token) return;
        current.pending = false;
        current.error = error;
        current.signature = null;
        const target = current.card;
        if (target?.dataset.signature === signature) {
          delete target.dataset.signature;
          if (target.isConnected && !current.data)
            showError(target.querySelector(".lineage-scroll"), error);
        }
        if (target?.isConnected && runs.some((run) => (run.project || "") === key))
          scheduleLineageRetry(key);
        else clearLineageRetry(key);
      });
  }
  function draw() {
    const active = document.activeElement;
    const focusedNode = active?.closest?.(".lineage-node");
    pendingFocus =
      focusedNode && content.contains(focusedNode)
        ? {
            runId: focusedNode.getAttribute("data-run-id") || "",
            project:
              focusedNode.closest("[data-project]")?.dataset.project ?? "",
          }
        : { runId: "", project: "" };
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
    for (const key of [...lineageCache.keys()]) {
      if (!groups.has(key)) {
        clearLineageRetry(key);
        lineageCache.delete(key);
      }
    }
    const shownProjects = new Set(
      projects.filter((key) => project === null || key === project),
    );
    for (const key of [...lineageRetry.keys()])
      if (!shownProjects.has(key)) clearLineageRetry(key);
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
    const finished = runs
      .map((run) => Date.parse(run.finished_at))
      .filter(Number.isFinite);
    count.textContent = [
      `${runs.length} run${runs.length === 1 ? "" : "s"}`,
      `${projects.length} project${projects.length === 1 ? "" : "s"}`,
      finished.length
        ? `last finished ${new Date(Math.max(...finished)).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`
        : null,
    ]
      .filter(Boolean)
      .join(" · ");
    updateSelection();
    const query = search.value.trim().toLowerCase();
    notebook.update({ project, query: filterWords(query) });
    const cards = [];
    for (const key of projects) {
      if (project !== null && key !== project) continue;
      const siblings = groups
        .get(key)
        .sort(
          (a, b) =>
            (Number.isFinite(Date.parse(b.created_at))
              ? Date.parse(b.created_at)
              : -Infinity) -
            (Number.isFinite(Date.parse(a.created_at))
              ? Date.parse(a.created_at)
              : -Infinity),
        );
      const signature = lineageSignature(siblings);
      const running = siblings.some((run) => run.status === "running");
      let card = [...content.children].find(
        (node) =>
          node.classList.contains("project-card") && node.dataset.project === key,
      );
      const unchanged = card && card.dataset.signature === signature;
      if (!card) {
        const scroll = el(
          "div",
          { class: "lineage-scroll" },
          loading("Loading lineage…"),
        );
        const detail = el(
          "div",
          { class: "lineage-detail" },
          el("p", {
            class: "muted",
            text: "Pick a run to see its parent, commit and changed files.",
          }),
        );
        card = el(
          "section",
          { class: "card project-card", "data-project": key },
          projectHead(key),
          el(
            "div",
            { class: "project-body" },
            el("div", { class: "runs-pane" }),
            el(
              "div",
              { class: "lineage-body" },
              el("div", { class: "lineage-head" }, eyebrow("Lineage")),
              scroll,
              detail,
            ),
          ),
        );
        bindLineageCard(card);
      }
      card.dataset.signature = signature;
      // Keep the graph DOM, scroller, focus and detail strip on ordinary polls.
      // Only changed signatures or the running-project refresh fetch/repaint it.
      if (unchanged) {
        dimLineage(card, siblings, query);
        const entry = lineageCache.get(key);
        if (
          running &&
          entry &&
          !entry.pending &&
          Date.now() - (entry.fetchedAt || 0) >= LINEAGE_REFRESH_MS
        )
          ensureLineage(key, signature, card, true);
      } else ensureLineage(key, signature, card, running);
      const visible = siblings.filter((run) => runMatches(run, query));
      const keys = variedKeys(siblings);
      const headlineKey = siblings.map(headline).find(Boolean) || "Metric";
      card.dataset.headline = headlineKey;
      projectMeta(card, siblings);
      const metricText =
        headlineKey === "Metric" ? "—" : metricLabel(headlineKey);
      const metric = card.querySelector(".project-metric");
      if (metric.dataset.value !== metricText) {
        metric.dataset.value = metricText;
        metric.replaceChildren(
          "primary metric · ",
          el("span", { class: "project-metric-name", text: metricText }),
        );
      }
      const columns = el(
        "div",
        { class: "run-grid run-columns" },
        el("span"),
        el("span", { text: "Run · changed vs parent" }),
        el("span", { class: "run-status", text: "Status" }),
        el("span", { class: "run-progress", text: "Last step" }),
        el("span", {
          class: "run-number run-metric",
          text: headlineKey === "Metric" ? headlineKey : metricLabel(headlineKey),
          title: headlineKey,
        }),
        el("span", { class: "run-number run-delta", text: "Δ vs parent" }),
        el("span", { class: "run-number run-duration", text: "Wall time" }),
        el("span", { class: "run-chevron" }),
      );
      const rows = visible.map((run) => {
        const pick = () => {
          selected.has(run.run_id)
            ? selected.delete(run.run_id)
            : selected.add(run.run_id);
          updateSelection();
        };
        const check = el("input", {
          type: "checkbox",
          role: "checkbox",
          class: "check",
          checked: selected.has(run.run_id),
          "aria-label": `Select ${run.name || run.run_id}`,
          onclick: (event) => event.stopPropagation(),
          onchange: pick,
          onkeydown: (event) => event.stopPropagation(),
        });
        const metricKey = headlineKey;
        const facts = parentFacts(run, metricKey, lineageCache.get(key)?.data, keys);
        const name = el("a", {
          class: "run-name",
          href: routeRun(run.run_id),
          text: run.name || run.run_id,
          title: run.name || run.run_id,
          onclick: (event) => event.stopPropagation(),
        });
        return el(
          "div",
          {
            class: "run-grid run-row",
            "data-run-id": run.run_id,
            "data-picked": selected.has(run.run_id),
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
              name,
              el("span", { class: "run-id", text: shortRunId(run.run_id) }),
            ),
            el("div", {
              class: "run-varied",
              text: facts.changed,
              title: facts.changed,
            }),
          ),
          el("div", { class: "run-status" }, statusChip(run.status, run)),
          el("span", {
            class: "muted run-progress",
            text:
              run.last_step == null
                ? "—"
                : run.last_step >= 1000
                  ? `${(run.last_step / 1000).toFixed(run.last_step % 1000 && run.last_step < 10000 ? 1 : 0)}k`
                  : String(run.last_step),
          }),
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
      });
      for (const peer of card.querySelectorAll(".is-linked"))
        peer.classList.remove("is-linked");
      card.querySelector(".runs-pane").replaceChildren(
        ...(visible.length
          ? [columns, ...rows]
          : [
              el("p", {
                class: "runs-empty muted",
                text: "No runs match this filter.",
              }),
            ]),
      );
      restoreLineageHover(card);
      cards.push(card);
    }
    if (cards.length && !cards.some((card) => card.querySelector(".run-row")))
      cards.unshift(el("p", { class: "empty", text: "No runs match this filter." }));
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
  }
  search.oninput = draw;
  async function refresh() {
    notebook.refresh();
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
    for (const key of [...lineageRetry.keys()]) clearLineageRetry(key);
    notebook.destroy();
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
function metricsPanel(initialRuns, compare, signal, { onMetrics } = {}) {
  const node = el("div", { class: "metrics-panel stack" });
  let runs = initialRuns,
    metrics = Object.create(null),
    charts = new Map(),
    expanded = null,
    focusChart = null,
    hover = null,
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
  let overlay = null,
    focusTable;
  const smoothCache = new Map();
  const metricCache = new Map();
  function smoothed(run, key) {
    const raw = metrics[run.run_id]?.[key] || [];
    const cached = smoothCache.get(run.run_id);
    if (cached?.raw === raw && cached.weight === slider.value)
      return cached.values;
    const values = emaSmooth(
      raw.map((point) => point.value),
      Number(slider.value),
    );
    smoothCache.set(run.run_id, { raw, weight: slider.value, values });
    return values;
  }
  function renderTable() {
    if (!expanded || !focusTable) return;
    focusTable.replaceChildren(
      table(
        [
          "Run",
          "Smoothed",
          "Value",
          hover == null ? "Step · latest" : "Step · hover",
        ],
        runs.flatMap((run) => {
          const raw = view()[run.run_id]?.[expanded] || [];
          if (!raw.length) return [];
          let i = raw.length - 1;
          if (hover != null) {
            i = 0;
            raw.forEach((point, index) => {
              if (Math.abs(point.step - hover) < Math.abs(raw[i].step - hover))
                i = index;
            });
          }
          return [
            tableRow([
              el(
                "span",
                { class: "inline mono run-label" },
                dot(colors[run.run_id]),
                run.name || run.run_id,
              ),
              fmt(smoothed(run, expanded)[i]),
              fmt(raw[i].value),
              fmt(raw[i].at ?? raw[i].step),
            ]),
          ];
        }),
      ),
    );
  }
  function open(key) {
    expanded = key;
    overlay = focus(
      key,
      runs.length > 1 ? `${runs.length} runs` : runs[0]?.name,
      "chart-focus",
      () => {
        focusChart?.destroy();
        focusChart = null;
        overlay = null;
        expanded = null;
        hover = null;
      },
    );
    const canvas = el("canvas", { "aria-label": `Metric chart ${key}` });
    focusTable = el("div", { class: "card", "data-testid": "hover-table" });
    overlay.body.append(
      el(
        "div",
        { class: "focus-content stack" },
        el("div", { class: "full-chart" }, canvas),
        focusTable,
      ),
    );
    focusChart = new MetricChart(canvas, {
      ...options(key),
      onHover: (step) => {
        hover = step;
        renderTable();
      },
    });
    renderTable();
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
  function updateFocus() {
    if (focusChart) {
      focusChart.update(options(expanded));
      renderTable();
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
      updateFocus();
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
                  "aria-label": `Expand ${key}`,
                  onclick: () => open(key),
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
    updateFocus();
  }
  slider.oninput = () => {
    smoothValue.textContent = Number(slider.value).toFixed(2);
    charts.forEach((chart) =>
      chart.update({ smoothing: Number(slider.value) }),
    );
    if (focusChart) {
      focusChart.update({ smoothing: Number(slider.value) });
      renderTable();
    }
  };
  outliers.onchange = () => {
    charts.forEach((chart) =>
      chart.update({ ignoreOutliers: outliers.checked }),
    );
    focusChart?.update({ ignoreOutliers: outliers.checked });
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
    destroy() {
      disposed = true;
      charts.forEach((chart) => chart.destroy());
      overlay?.close();
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
  const panel = metricsPanel([run], false, signal, {
    onMetrics: (metrics) => {
      lastValues = metrics[id] || {};
      drawStats();
    },
  });
  const notepad = createNotepad({ runId: id, el, button, signal });
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
      notepad.node,
      el(
        "div",
        { class: "stack" },
        el(
          "div",
          { class: "heading" },
          eyebrow("Metrics"),
          el("span", { class: "muted", text: "Click a chart to expand" }),
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
    notepad.flush();
    notepad.destroy();
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
