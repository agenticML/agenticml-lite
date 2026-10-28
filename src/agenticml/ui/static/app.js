/* AgenticML local dashboard. Store strings enter the DOM only as text. */
import { MetricChart, emaSmooth, colorForId, formatNumber } from "./charts.js";
import { createCodeViewer } from "./code.js";

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
const statusChip = (status) =>
  chip(
    status || "unknown",
    { running: "accent", finished: "ok", crashed: "danger", stale: "warn" }[
      status
    ],
  );
const eyebrow = (text) => el("div", { class: "eyebrow", text });
const dot = (color) =>
  el("span", { class: "dot", style: `background:${color}` });
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
const lowerBetter = (key) => /loss|err|error/i.test(key);
function duration(run) {
  if (!run.created_at) return "—";
  const seconds = Math.max(
    0,
    ((run.finished_at ? new Date(run.finished_at) : Date.now()) -
      new Date(run.created_at)) /
      1000,
  );
  if (!Number.isFinite(seconds)) return "—";
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
  if (error.name !== "AbortError")
    node.replaceChildren(el("p", { class: "error", text: error.message }));
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
          'button:not(:disabled), input, a[href], [tabindex="0"]',
        ),
      ].filter((node) => node.getClientRects().length);
      const index = targets.indexOf(document.activeElement);
      if (
        (event.shiftKey && index <= 0) ||
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
async function home(version) {
  const selected = new Set();
  const search = el("input", {
    type: "search",
    class: "field",
    placeholder: "Filter runs…",
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
  const status = el("span", { class: "poll-status" });
  const count = el("span", { class: "muted" });
  app.replaceChildren(
    el(
      "div",
      { class: "workspace stack" },
      el(
        "div",
        { class: "heading" },
        el("div", {}, eyebrow("Workspace"), el("h1", { text: "Runs" })),
        count,
      ),
      tabs,
      el("div", { class: "selection-slot" }, selection),
      content,
      status,
    ),
  );
  let runs = [],
    project = null,
    timer;
  const selectionCount = el("span", { text: "Select runs to compare" });
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
  selection.append(selectionCount, selectionActions);
  function updateSelection() {
    selection.classList.toggle("is-empty", !selected.size);
    selectionActions.hidden = !selected.size;
    selectionCount.textContent = selected.size
      ? `${selected.size} run${selected.size === 1 ? "" : "s"} selected`
      : "Select runs to compare";
    compareButton.disabled = selected.size < 2;
    for (const row of content.querySelectorAll(".run-row")) {
      const picked = selected.has(row.dataset.runId);
      row.dataset.picked = picked;
      row.querySelector(".check").checked = picked;
    }
  }
  function draw() {
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
    for (const tab of tabs.querySelectorAll("button")) tab.remove();
    tabs.prepend(
      ...[null, ...projects].map((key) =>
        button(
          key === null ? "All" : key || "No project",
          () => {
            project = key;
            draw();
          },
          "ghost",
          { role: "tab", "aria-selected": key === project },
        ),
      ),
    );
    if (!search.parentNode) tabs.append(search);
    count.textContent = `${runs.length} runs · grouped by project`;
    updateSelection();
    const query = search.value.trim().toLowerCase();
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
      const visible = siblings.filter((run) =>
        `${run.name} ${run.run_id} ${(run.tags || []).join(" ")}`
          .toLowerCase()
          .includes(query),
      );
      if (!visible.length) continue;
      const keys = variedKeys(siblings);
      const headlineKey = siblings.map(headline).find(Boolean) || "Metric";
      // Walk oldest first, batching equal timestamps so only strictly earlier
      // finite samples can serve as a baseline. This is linear after sorting.
      const previousValues = new Map();
      let prior = null;
      for (let end = siblings.length; end > 0;) {
        let start = end - 1;
        const time = Date.parse(siblings[start].created_at);
        while (start > 0 && Date.parse(siblings[start - 1].created_at) === time)
          start--;
        for (let i = start; i < end; i++)
          previousValues.set(
            siblings[i].run_id,
            Number.isFinite(time) ? prior : null,
          );
        if (Number.isFinite(time))
          for (let i = start; i < end; i++)
            if (isNumber(siblings[i].summary?.[headlineKey]))
              prior = siblings[i].summary[headlineKey];
        end = start;
      }
      const columns = el(
        "div",
        { class: "run-grid run-columns" },
        el("span"),
        el("span", { text: "Run · varied" }),
        el("span", { class: "run-progress", text: "Progress" }),
        el("span", {
          class: "run-number",
          text: headlineKey,
          title: headlineKey,
        }),
        el("span", { class: "run-number run-delta", text: "Δ" }),
        el("span", { class: "run-number run-duration", text: "Duration" }),
        el("span"),
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
        const previous = previousValues.get(run.run_id);
        const delta =
          previous != null && isNumber(run.summary?.[metricKey])
            ? run.summary[metricKey] - previous
            : null;
        const tone =
          delta == null || delta === 0
            ? ""
            : delta < 0 === lowerBetter(metricKey)
              ? "ok"
              : "warn";
        const name = el("a", {
          class: "run-name",
          href: routeRun(run.run_id),
          text: run.name || run.run_id,
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
            el("div", { class: "inline" }, name, statusChip(run.status)),
            el("div", {
              class: "run-varied",
              text: varied(run, keys),
              title: varied(run, keys),
            }),
          ),
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
            class: "run-number",
            title: metricKey || "",
            text: fmt(run.summary?.[metricKey]),
          }),
          el("span", {
            class: `run-number run-delta ${tone}`,
            text: delta == null ? "—" : `${delta > 0 ? "+" : ""}${fmt(delta)}`,
          }),
          el("span", { class: "run-number run-duration", text: duration(run) }),
          el("span", { class: "muted", text: "›" }),
        );
      });
      cards.push(
        el(
          "section",
          { class: "card project-card", "data-project": key },
          el(
            "header",
            { class: "project-head inline" },
            el("h2", { text: key || "No project" }),
            !key
              ? el("span", {
                  class: "project-note",
                  text: "no project — pass project= or run inside a repo",
                })
              : null,
          ),
          columns,
          ...rows,
        ),
      );
    }
    content.replaceChildren(
      ...(cards.length
        ? cards
        : [
            el("p", {
              class: "empty",
              text: runs.length
                ? "No runs match this filter."
                : "No runs found. Start a local experiment to see it here.",
            }),
          ]),
    );
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
      status.textContent = live
        ? "Live · refreshes every 5 s"
        : `${runs.length} runs`;
      if (live) timer = setTimeout(refresh, 5000);
    } catch (error) {
      if (epoch === version) {
        showError(content, error);
        timer = setTimeout(refresh, 5000);
      }
    }
  }
  cleanup = () => clearTimeout(timer);
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
function metricsPanel(initialRuns, compare, signal) {
  const node = el("div", { class: "metrics-panel stack" });
  let runs = initialRuns,
    metrics = Object.create(null),
    charts = new Map(),
    expanded = null,
    focusChart = null,
    hover = null,
    disposed = false;
  const colors = Object.fromEntries(
    runs.map((run, index) => [
      run.run_id,
      compare ? `var(--s${(index % 5) + 1})` : colorForId(run.run_id),
    ]),
  );
  const chartRuns = () => runs.map((run) => ({ ...run, id: run.run_id }));
  const slider = el("input", {
    type: "range",
    min: 0,
    max: 0.99,
    step: 0.01,
    value: 0.6,
    "aria-label": "Smoothing",
  });
  const smoothValue = el("span", { class: "mono", text: "0.60" });
  const outliers = el("input", {
    type: "checkbox",
    checked: true,
    "aria-label": "Ignore outliers",
  });
  const filter = el("input", {
    class: "field",
    type: "search",
    placeholder: "Filter tags…",
    "aria-label": "Filter tags",
  });
  const count = el("span", { class: "muted" }),
    content = el("div", { class: "stack" }),
    note = el("div", { class: "muted" }),
    failure = el("div");
  node.append(
    el(
      "div",
      { class: "card toolbar" },
      el("label", {}, eyebrow("Smoothing"), slider, smoothValue),
      el("label", {}, outliers, "Ignore outliers"),
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
            { class: "inline" },
            dot(colors[run.run_id]),
            run.name || run.run_id,
          ),
        ),
      ),
    );
  node.append(failure, content, note);
  const options = (key) => ({
    runs: chartRuns(),
    metrics,
    activeMetric: key,
    smoothing: Number(slider.value),
    ignoreOutliers: outliers.checked,
    colors,
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
          const raw = metrics[run.run_id]?.[expanded] || [];
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
                { class: "inline mono" },
                dot(colors[run.run_id]),
                run.name || run.run_id,
              ),
              fmt(smoothed(run, expanded)[i]),
              fmt(raw[i].value),
              fmt(raw[i].step),
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
    const shown = keys.filter((key) =>
      key.toLowerCase().includes(filter.value.trim().toLowerCase()),
    );
    count.textContent = `${shown.length} / ${keys.length} tags`;
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
                  el("span", {
                    text: key.includes("/")
                      ? key.slice(key.indexOf("/") + 1)
                      : key,
                  }),
                  el("span", { class: "chart-key", text: `${key} ⤢` }),
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
              el("h3", { text: group }),
              el("span", { class: "muted", text: groupKeys.length }),
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
            ? "No tags match this filter."
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
    failure.replaceChildren(
      ...errors.map((text) => el("p", { class: "error", text })),
    );
    note.textContent = truncated
      ? "Some metric keys were truncated by the server."
      : "";
    if (changed || !charts.size) draw();
  }
  content.append(el("p", { class: "muted", text: "Loading metrics…" }));
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
      Created: date(run.created_at),
      Finished: date(run.finished_at),
      Duration: duration(run),
      Host: run.hostname || "—",
      "Git commit": el(
        "span",
        { class: "inline mono git-identity" },
        el("span", { text: `${git.git_commit?.slice(0, 8) || "—"} ·` }),
        el("span", { text: git.git_branch || "—" }),
        git.is_dirty ? chip("dirty", "warn") : null,
      ),
    },
    true,
  );
}
async function runPage(id, version) {
  const signal = controller.signal;
  const [detail, summaries, initialMedia] = await Promise.all([
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
  const siblings = summaries.filter(
    (other) => (other.project || "") === (run.project || ""),
  );
  const title = el("div", { class: "inline" }),
    subline = el("div", { class: "subline" }),
    facts = el("div", { "data-testid": "identity" }),
    anatomy = el("div", { class: "anatomy" });
  const pollStatus = el("span", { class: "poll-status" }),
    pollError = el("div");
  const codeContainer = el(
    "div",
    { class: "code-container" },
    el("p", { class: "muted", text: "Loading code snapshot…" }),
  );
  const gitAside = el("span", { class: "muted", text: "Finding baseline…" });
  const parameterSearch = el("input", {
    class: "field",
    type: "search",
    placeholder: "Filter parameters…",
    "aria-label": "Search Parameters",
  });
  const parameterValues = el("div");
  const filterParameters = () => {
    const query = parameterSearch.value.toLowerCase();
    for (const row of parameterValues.querySelectorAll(".kv-list > div")) {
      row.hidden = !row.textContent.toLowerCase().includes(query);
    }
  };
  parameterSearch.oninput = filterParameters;
  let code = null,
    timer;
  const panel = metricsPanel([run], false, signal);
  function drawFacts() {
    title.replaceChildren(
      el("h1", { class: "mono", text: run.name || id }),
      statusChip(run.status),
      ...(run.tags || []).map((tag) => chip(tag)),
    );
    subline.textContent = [
      varied(run, variedKeys(siblings)),
      run.hostname,
      date(run.created_at),
    ]
      .filter(Boolean)
      .join(" · ");
    subline.title = subline.textContent;
    facts.replaceChildren(identity(run));
    const env = { ...(run.env || {}) },
      packages = env.packages;
    delete env.packages;
    const environment = el("div", { class: "stack" }, kv(env));
    if (packages)
      environment.append(
        el(
          "details",
          {},
          el("summary", { text: "Packages" }),
          el("pre", {
            text:
              typeof packages === "object"
                ? JSON.stringify(packages, null, 2)
                : String(packages),
          }),
        ),
      );
    const git = run.git || {};
    const artifacts = el(
      "div",
      { class: "stack" },
      ...(run.artifacts || []).map((artifact) =>
        el(
          "div",
          { class: "inline", "data-artifact": artifact.name },
          el("span", {
            class: "mono",
            text: artifact.name || artifact.path || "artifact",
          }),
          el("span", {
            class: "muted",
            text: `${artifact.size == null ? "—" : formatBytes(artifact.size)}${artifact.type ? ` · ${artifact.type}` : ""}${artifact.version == null ? "" : ` · v${artifact.version}`}`,
          }),
        ),
      ),
    );
    if (!artifacts.children.length)
      artifacts.append(el("span", { class: "muted", text: "—" }));
    parameterValues.replaceChildren(kv(run.config));
    filterParameters();
    anatomy.replaceChildren(
      card(
        "Parameters",
        el("div", { class: "stack" }, parameterSearch, parameterValues),
      ),
      card("Metrics", kv(run.summary)),
      card(
        "Git commit",
        kv({
          Commit: git.git_commit,
          Branch: git.git_branch,
          State: git.is_dirty ? "dirty" : "clean",
        }),
        gitAside,
      ),
      card("Environment", environment),
      card("Artifacts", artifacts),
      mediaCard(media),
    );
    pollStatus.textContent =
      run.status === "running" ? "Live · refreshes every 5 s" : "Up to date";
  }
  const fullButton = button("Open full window ⤢", () => code?.openFull());
  app.replaceChildren(
    el(
      "div",
      { class: "detail stack" },
      el("a", { href: "#/", text: "← All runs" }),
      title,
      subline,
      facts,
      el(
        "div",
        { class: "stack" },
        el(
          "div",
          { class: "heading" },
          eyebrow("Metrics"),
          el("span", { class: "muted", text: "click a chart to expand" }),
        ),
        panel.node,
      ),
      el("div", { class: "stack" }, eyebrow("Run anatomy"), anatomy),
      el(
        "div",
        { class: "stack" },
        el("div", { class: "heading" }, eyebrow("Code"), fullButton),
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
    onBaseline: (baseline) => {
      if (epoch !== version) return;
      gitAside.replaceChildren(
        baseline
          ? button("View diff", () => code?.openFull())
          : el("span", { text: "no baseline" }),
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
function comparisonTable(runs, colors) {
  const keys = variedKeys(runs);
  const rows = [];
  function section(text) {
    rows.push(
      el(
        "tr",
        {},
        el("td", { colSpan: runs.length + 1, class: "table-section", text }),
      ),
    );
  }
  section(
    keys.length ? `Params that differ (${keys.length})` : "No params differ",
  );
  for (const key of keys)
    rows.push(tableRow([key, ...runs.map((run) => fmt(run.config?.[key]))]));
  section("Metrics");
  const metrics = [
    ...new Set(runs.flatMap((run) => Object.keys(run.summary || {}))),
  ].sort();
  for (const key of metrics) {
    const values = runs.map((run) => run.summary?.[key]);
    const numeric = values.filter(isNumber);
    const best = numeric.length
      ? lowerBetter(key)
        ? Math.min(...numeric)
        : Math.max(...numeric)
      : null;
    rows.push(
      tableRow([
        key,
        ...values.map((value) =>
          el("span", {
            class: `mono ${isNumber(value) && value === best ? "ok" : ""}`,
            text: fmt(value),
          }),
        ),
      ]),
    );
  }
  const node = table(["", ...runs.map((run) => run.name || run.run_id)], rows);
  const headers = node.querySelectorAll("th");
  runs.forEach((run, index) =>
    headers[index + 1].prepend(
      dot(colors[run.run_id]),
      document.createTextNode(" "),
    ),
  );
  return node;
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
  const overlay = focus(
    "Compare runs",
    `${runs.length} runs selected`,
    "compare-focus",
    () => {
      if (epoch === version) location.hash = "#/";
    },
  );
  app.replaceChildren();
  let timer;
  const panel = metricsPanel(runs, true, signal);
  cleanup = () => {
    clearTimeout(timer);
    panel.destroy();
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
  const colors = Object.fromEntries(
    runs.map((run, index) => [run.run_id, `var(--s${(index % 5) + 1})`]),
  );
  const selected = el(
    "div",
    { class: "inline", "data-testid": "selected-runs" },
    ...runs.map((run) =>
      el(
        "span",
        {
          class: "chip inline",
          style: `background:color-mix(in srgb,${colors[run.run_id]} 10%,var(--surface-3));border:1px solid color-mix(in srgb,${colors[run.run_id]} 40%,transparent);padding:4px 10px`,
        },
        dot(colors[run.run_id]),
        run.name || run.run_id,
      ),
    ),
  );
  const comparison = el("div", { class: "card anatomy-card" }),
    config = el("div", { class: "card anatomy-card" }),
    pollError = el("div"),
    pollStatus = el("span", { class: "poll-status" });
  function draw() {
    comparison.replaceChildren(comparisonTable(runs, colors));
    config.replaceChildren(
      el("pre", {
        text: JSON.stringify(
          Object.fromEntries(
            runs.map((run) => [
              run.name || run.run_id.slice(0, 8),
              run.config || {},
            ]),
          ),
          null,
          2,
        ),
      }),
    );
    pollStatus.textContent = runs.some((run) => run.status === "running")
      ? "Live · refreshes every 5 s"
      : "Up to date";
  }
  overlay.body.append(
    el(
      "div",
      { class: "focus-content stack" },
      selected,
      el(
        "section",
        { class: "stack" },
        eyebrow("Comparative · all metrics"),
        panel.node,
      ),
      el(
        "section",
        { class: "stack" },
        el("h2", { class: "eyebrow", text: "Params & metrics comparison" }),
        comparison,
      ),
      el(
        "section",
        { class: "stack" },
        el("h2", { class: "eyebrow", text: "Config" }),
        config,
      ),
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
  app.replaceChildren(el("p", { class: "muted", text: "Loading runs…" }));
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
