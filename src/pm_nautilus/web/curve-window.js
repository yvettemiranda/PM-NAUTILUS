export const CURVE_WEEK_MS = 7 * 24 * 60 * 60 * 1000;

export function curveTimeBounds(points) {
  let from = Infinity, to = -Infinity;
  for (const point of points) {
    if (!Number.isFinite(point?.at)) continue;
    from = Math.min(from, point.at);
    to = Math.max(to, point.at);
  }
  return from === Infinity ? null : { from, to };
}

// A null end follows new samples; a numeric end preserves the viewed history.
export function resolveCurveWindow(bounds, end = null, duration = CURVE_WEEK_MS) {
  if (!Number.isFinite(bounds?.from) || !Number.isFinite(bounds?.to) || bounds.to < bounds.from) return null;
  const span = Math.min(Number.isFinite(duration) && duration > 0 ? duration : CURVE_WEEK_MS, bounds.to - bounds.from);
  const to = Math.max(bounds.from + span, Math.min(bounds.to, Number.isFinite(end) ? end : bounds.to));
  const from = to - span;
  return {
    from, to, historyFrom: bounds.from, historyTo: bounds.to,
    atLatest: to === bounds.to, canOlder: from > bounds.from, canNewer: to < bounds.to,
  };
}

// Dragging the plot right reveals older history; use the drag-start window.
export function panCurveWindow(window, deltaPixels, plotWidth) {
  if (!window || !Number.isFinite(deltaPixels) || !Number.isFinite(plotWidth) || plotWidth <= 0) return window?.to ?? null;
  const span = window.to - window.from;
  return Math.max(window.historyFrom + span, Math.min(window.historyTo, window.to - deltaPixels / plotWidth * span));
}

// Keep adjacent edge samples for clipped lines, including every intervening gap.
export function curveWindowPoints(points, window) {
  if (!window) return [];
  let first = points.length, last = -1;
  for (let index = 0; index < points.length; index += 1) {
    const at = points[index]?.at;
    if (!Number.isFinite(at)) continue;
    if (at >= window.from && at <= window.to) {
      first = Math.min(first, index);
      last = Math.max(last, index);
    }
    if (index === 0) continue;
    const previousAt = points[index - 1]?.at;
    if (Number.isFinite(previousAt) && previousAt < at && previousAt <= window.to && at >= window.from) {
      first = Math.min(first, index - 1);
      last = Math.max(last, index);
    }
  }
  return last < first ? [] : points.slice(first, last + 1);
}
