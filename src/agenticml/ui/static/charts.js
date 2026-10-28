/* Canvas metrics: hosted chart rules, with explicit lifecycle cleanup. */
const PAD = { top: 28, right: 28, bottom: 52, left: 68 };
const PAD_COMPACT = { top: 10, right: 10, bottom: 24, left: 44 };
export const formatNumber = (value) =>
  Number(value).toLocaleString([], { maximumFractionDigits: 4 });
export const readToken = (name, fallback = "#2f6fdb") =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() ||
  fallback;
export function colorForId(id) {
  const hash = [...id].reduce(
    (h, c) => ((h << 5) - h + c.charCodeAt(0)) | 0,
    0,
  );
  return `var(--s${(Math.abs(hash) % 5) + 1})`;
}
const resolveColor = (color) =>
  color.startsWith("var(") ? readToken(color.slice(4, -1)) : color;
const resolveColorForId = (id) => resolveColor(colorForId(id));
export function emaSmooth(values, weight) {
  if (weight <= 0) return values;
  let running = 0,
    count = 0;
  return values.map((v) => {
    if (typeof v !== "number" || !Number.isFinite(v)) {
      running = 0;
      count = 0;
      return null;
    }
    running = running * weight + (1 - weight) * v;
    return running / (1 - Math.pow(weight, ++count));
  });
}
function scale(v, min, max, outMin, outMax) {
  return min === max
    ? (outMin + outMax) / 2
    : outMin + ((v - min) / (max - min)) * (outMax - outMin);
}
export class MetricChart {
  constructor(canvas, options) {
    this.canvas = canvas;
    this.options = options;
    this.hoverX = null;
    this.paint = this.paint.bind(this);
    this.observer = new ResizeObserver(this.paint);
    this.observer.observe(canvas);
    window.addEventListener("dashboard-theme", this.paint);
    this.move = (e) => {
      if (!this.layout || this.options.compact) return;
      const l = this.layout;
      const f = Math.min(
        1,
        Math.max(
          0,
          (e.clientX - canvas.getBoundingClientRect().left - l.left) /
            (l.right - l.left),
        ),
      );
      this.hoverX = f;
      this.options.onHover?.(l.minStep + f * (l.maxStep - l.minStep));
      this.paintOverlay?.();
    };
    this.leave = () => {
      this.hoverX = null;
      this.options.onHover?.(null);
      this.paintOverlay?.();
    };
    canvas.addEventListener("mousemove", this.move);
    canvas.addEventListener("mouseleave", this.leave);
    this.paint();
  }
  update(options) {
    this.options = { ...this.options, ...options };
    this.paint();
  }
  destroy() {
    this.observer.disconnect();
    window.removeEventListener("dashboard-theme", this.paint);
    this.canvas.removeEventListener("mousemove", this.move);
    this.canvas.removeEventListener("mouseleave", this.leave);
    this.paintOverlay = null;
  }
  paint() {
    const canvas = this.canvas;
    const {
      runs,
      metrics,
      activeMetric,
      smoothing = 0,
      ignoreOutliers = false,
      compact = false,
      colors,
    } = this.options;
    this.layout = null;
    this.paintOverlay = null;
    const PAD_ = compact ? PAD_COMPACT : PAD;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (!canvas.isConnected || !rect.width || !rect.height) return;
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = canvas.width / dpr;
    const H = canvas.height / dpr;

    // Canvas cannot resolve CSS variables — read the literal values each paint.
    const tk = {
      surface: readToken("--surface", "#ffffff"),
      grid: readToken("--grid", "#eef0f1"),
      text4: readToken("--text-4", "#9aa2a8"),
      ghost: readToken("--ghost", "#cbd0d3"),
    };

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = tk.surface;
    ctx.fillRect(0, 0, W, H);

    const drawGrid = () => {
      ctx.strokeStyle = tk.grid;
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = PAD_.top + ((H - PAD_.top - PAD_.bottom) * i) / 4;
        ctx.beginPath();
        ctx.moveTo(PAD_.left, y);
        ctx.lineTo(W - PAD_.right, y);
        ctx.stroke();
      }
    };

    if (!activeMetric || !runs.length) {
      drawGrid();
      ctx.fillStyle = tk.ghost;
      ctx.font = "13px 'Instrument Sans', ui-sans-serif, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No chart data", W / 2, H / 2);
      return;
    }

    const series = runs
      .map((run) => {
        const raw = (metrics[run.id]?.[activeMetric] ?? []).map((p) => ({
          step: Number(p.step),
          value:
            typeof p.value === "number" && Number.isFinite(p.value)
              ? p.value
              : null,
        }));
        const sm = emaSmooth(
          raw.map((p) => p.value),
          smoothing,
        );
        return {
          run,
          raw,
          points: raw.map((p, i) => ({ step: p.step, value: sm[i] })),
        };
      })
      .filter((s) => s.points.some((point) => point.value != null));

    if (!series.length) {
      drawGrid();
      ctx.fillStyle = tk.ghost;
      ctx.font = "13px 'Instrument Sans', ui-sans-serif, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No chart data", W / 2, H / 2);
      return;
    }

    let minStep = Infinity,
      maxStep = -Infinity;
    const vals = [];
    for (const s of series)
      for (const point of s.raw) {
        minStep = Math.min(minStep, point.step);
        maxStep = Math.max(maxStep, point.step);
        if (point.value != null) vals.push(point.value);
      }
    // Sort once per data/control/size draw, never during hover.
    vals.sort((a, b) => a - b);
    const q = (f) =>
      vals[
        Math.min(
          vals.length - 1,
          Math.max(0, Math.round(f * (vals.length - 1))),
        )
      ];
    let minVal = ignoreOutliers ? q(0.02) : vals[0];
    let maxVal = ignoreOutliers ? q(0.98) : vals[vals.length - 1];
    if (ignoreOutliers && minVal === maxVal) {
      minVal = vals[0];
      maxVal = vals[vals.length - 1];
    }
    const pW = W - PAD_.left - PAD_.right;
    const pH = H - PAD_.top - PAD_.bottom;

    drawGrid();

    const sx = (step) =>
      scale(step, minStep, maxStep, PAD_.left, PAD_.left + pW);
    const sy = (value) =>
      Math.min(
        PAD_.top + pH,
        Math.max(
          PAD_.top,
          scale(value, minVal, maxVal, PAD_.top + pH, PAD_.top),
        ),
      );

    // A lone series gets the primary hue and its area fill; multiples never do
    // — two translucent areas overlapping is unreadable.
    const single = series.length === 1;

    for (const s of series) {
      const color =
        single && !colors
          ? readToken("--s1", "#2f6fdb")
          : colors?.[s.run.id]
            ? resolveColor(colors[s.run.id])
            : resolveColorForId(s.run.id);

      // Both strokes and area fills stop at missing samples.
      const segments = (points) => {
        const result = [];
        let segment = [];
        for (const point of points) {
          if (point.value == null) {
            if (segment.length) result.push(segment);
            segment = [];
          } else segment.push(point);
        }
        if (segment.length) result.push(segment);
        return result;
      };
      const smoothSegments = segments(s.points);
      if (single) {
        for (const points of smoothSegments) {
          ctx.beginPath();
          points.forEach((p, i) =>
            i
              ? ctx.lineTo(sx(p.step), sy(p.value))
              : ctx.moveTo(sx(p.step), sy(p.value)),
          );
          ctx.lineTo(sx(points.at(-1).step), H - PAD_.bottom);
          ctx.lineTo(sx(points[0].step), H - PAD_.bottom);
          ctx.closePath();
          ctx.fillStyle = readToken("--area", "rgba(47,111,219,0.10)");
          ctx.fill();
        }
      }
      const stroke = (groups) => {
        ctx.beginPath();
        for (const points of groups)
          points.forEach((p, i) =>
            i
              ? ctx.lineTo(sx(p.step), sy(p.value))
              : ctx.moveTo(sx(p.step), sy(p.value)),
          );
        ctx.stroke();
      };
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      if (smoothing > 0) {
        ctx.save();
        ctx.globalAlpha = 0.22;
        stroke(segments(s.raw));
        ctx.restore();
      }
      stroke(smoothSegments);

      // Marker on the last point only.
      const last = smoothSegments.at(-1).at(-1);
      const lx = sx(last.step),
        ly = sy(last.value);
      ctx.fillStyle = tk.surface;
      ctx.beginPath();
      ctx.arc(lx, ly, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(lx, ly, 3.5, 0, Math.PI * 2);
      ctx.stroke();
    }

    this.layout = { left: PAD_.left, right: W - PAD_.right, minStep, maxStep };
    // Axis labels stay mono.
    ctx.fillStyle = tk.text4;
    ctx.font = `${compact ? 9.5 : 11}px 'JetBrains Mono', ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.fillText(
      compact ? formatNumber(minStep) : `step ${formatNumber(minStep)}`,
      PAD_.left,
      H - (compact ? 8 : 18),
    );
    ctx.fillText(
      compact ? formatNumber(maxStep) : `step ${formatNumber(maxStep)}`,
      W - PAD_.right,
      H - (compact ? 8 : 18),
    );
    ctx.textAlign = "right";
    ctx.fillText(formatNumber(maxVal), PAD_.left - 8, PAD_.top + 4);
    ctx.fillText(formatNumber(minVal), PAD_.left - 8, H - PAD_.bottom + 4);

    if (compact) return;
    // Keep the static chart in a backing canvas. Mouse movement only restores
    // those pixels and paints the crosshair/markers using this draw's data.
    const backing = document.createElement("canvas");
    backing.width = canvas.width;
    backing.height = canvas.height;
    backing.getContext("2d").drawImage(canvas, 0, 0);
    this.paintOverlay = () => {
      ctx.drawImage(backing, 0, 0, W, H);
      const hoverX = this.hoverX;
      if (hoverX != null && !compact) {
        const hx = PAD_.left + hoverX * pW;
        const hs = minStep + hoverX * (maxStep - minStep);
        ctx.save();
        ctx.strokeStyle = tk.text4;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(hx, PAD_.top);
        ctx.lineTo(hx, PAD_.top + pH);
        ctx.stroke();
        ctx.restore();
        for (const s of series) {
          let best = null;
          for (const p of s.points)
            if (
              p.value != null &&
              (!best || Math.abs(p.step - hs) < Math.abs(best.step - hs))
            )
              best = p;
          if (!best || best.step > hs + (maxStep - minStep) * 0.02) continue;
          const color = colors?.[s.run.id]
            ? resolveColor(colors[s.run.id])
            : resolveColorForId(s.run.id);
          ctx.fillStyle = tk.surface;
          ctx.strokeStyle = color;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(hx, sy(best.value), 4, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }
      }
    };
    if (this.hoverX != null) this.paintOverlay();
  }
}
