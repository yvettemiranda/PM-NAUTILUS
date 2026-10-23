import test from 'node:test';
import assert from 'node:assert/strict';
import { CURVE_WEEK_MS, curveTimeBounds, resolveCurveWindow, panCurveWindow, curveWindowPoints } from '../src/pm_nautilus/web/curve-window.js';
import { curveSegments } from '../src/pm_nautilus/web/ui-state.js';

const DAY = CURVE_WEEK_MS / 7;
const point = (day, pnl = '1', extra = {}) => ({ at: day * DAY, pnl, session: 'a', bucket: day, ...extra });

test('default viewport displays only the latest seven days without changing history', () => {
  const points = Array.from({ length: 31 }, (_, index) => point(index));
  const snapshot = structuredClone(points);
  assert.deepEqual(resolveCurveWindow(curveTimeBounds(points)), {
    from: 23 * DAY, to: 30 * DAY, historyFrom: 0, historyTo: 30 * DAY,
    atLatest: true, canOlder: true, canNewer: false,
  });
  assert.deepEqual(points, snapshot);
});

test('short and single-sample histories remain within their actual bounds', () => {
  assert.deepEqual(resolveCurveWindow({ from: DAY, to: 3 * DAY }), {
    from: DAY, to: 3 * DAY, historyFrom: DAY, historyTo: 3 * DAY,
    atLatest: true, canOlder: false, canNewer: false,
  });
  const single = resolveCurveWindow(curveTimeBounds([point(2)]));
  assert.equal(single.from, 2 * DAY);
  assert.equal(single.to, 2 * DAY);
  assert.equal(panCurveWindow(single, 100, 700), 2 * DAY);
});

test('empty and malformed timestamps cannot create an invalid viewport', () => {
  assert.equal(curveTimeBounds([]), null);
  assert.equal(curveTimeBounds([{}, { at: NaN }, { at: '100' }, null]), null);
  assert.deepEqual(curveTimeBounds([point(2), { at: Infinity }, point(0)]), { from: 0, to: 2 * DAY });
  for (const bounds of [null, {}, { from: 2, to: 1 }, { from: 0, to: Infinity }]) assert.equal(resolveCurveWindow(bounds), null);
  assert.deepEqual(curveWindowPoints([], null), []);
});

test('dragging right moves backward by the time represented by the drag distance', () => {
  const start = resolveCurveWindow({ from: 0, to: 30 * DAY });
  const end = panCurveWindow(start, 200, 700);
  assert.equal(end, 28 * DAY);
  assert.equal(resolveCurveWindow({ from: 0, to: 30 * DAY }, end).from, 21 * DAY);
  assert.equal(panCurveWindow(start, -200, 700), 30 * DAY);
  assert.equal(panCurveWindow(start, 10000, 700), 7 * DAY);
});

test('drag bounds and bad pointer measurements never overshoot the stored history', () => {
  const start = resolveCurveWindow({ from: DAY, to: 30 * DAY }, 7 * DAY);
  assert.equal(start.from, DAY);
  assert.equal(start.to, 8 * DAY);
  assert.equal(panCurveWindow(start, 100, 700), 8 * DAY);
  assert.equal(panCurveWindow(start, -10000, 700), 30 * DAY);
  for (const width of [0, -1, NaN, Infinity]) assert.equal(panCurveWindow(start, 100, width), start.to);
  assert.equal(panCurveWindow(start, NaN, 700), start.to);
  assert.equal(panCurveWindow(null, 10, 100), null);
});

test('refresh follows the newest sample only when the end anchor is null', () => {
  const refreshed = { from: 0, to: 31 * DAY };
  assert.equal(resolveCurveWindow(refreshed).to, 31 * DAY);
  assert.equal(resolveCurveWindow(refreshed, 20 * DAY).to, 20 * DAY);
  assert.equal(resolveCurveWindow(refreshed, 100 * DAY).to, 31 * DAY);
  assert.equal(resolveCurveWindow(refreshed, -100 * DAY).to, 7 * DAY);
});

test('H/D switches retain the same time window when using the common history bounds', () => {
  const hourly = [point(0), point(15.5), point(30.5)];
  const daily = [point(0.5), point(15), point(30)];
  const bounds = curveTimeBounds([...hourly, ...daily]);
  const window = resolveCurveWindow(bounds, 16 * DAY);
  assert.equal(window.from, 9 * DAY);
  assert.equal(window.to, 16 * DAY);
  assert.deepEqual(resolveCurveWindow(bounds, window.to), window);
  assert.equal(resolveCurveWindow(curveTimeBounds([point(100)]), null).to, 100 * DAY);
});

test('edge samples are included without mutating or dropping unknown points', () => {
  const points = [point(0), point(1), point(2, null), point(3), point(4)];
  const slice = curveWindowPoints(points, { from: 1.5 * DAY, to: 2.5 * DAY });
  assert.deepEqual(slice, points.slice(1, 4));
  assert.equal(slice[1], points[2]);
  assert.deepEqual(curveSegments(slice).map(segment => segment.map(p => p.at)), [[DAY], [3 * DAY]]);
  assert.equal(points.length, 5);
});

test('sparse edge neighbors do not invent a line over missing samples', () => {
  const points = [point(0), point(10)];
  const slice = curveWindowPoints(points, { from: 2 * DAY, to: 3 * DAY });
  assert.deepEqual(slice, points);
  assert.deepEqual(curveSegments(slice).map(segment => segment.length), [1, 1]);
  assert.deepEqual(curveWindowPoints(points, { from: 11 * DAY, to: 12 * DAY }), []);
});

test('adjacent samples retain a boundary crossing while restarts stay disconnected', () => {
  const connected = [point(0), point(1), point(2)];
  const window = { from: 0.25 * DAY, to: 0.75 * DAY };
  assert.deepEqual(curveWindowPoints(connected, window), connected.slice(0, 2));
  assert.deepEqual(curveSegments(curveWindowPoints(connected, window)).map(segment => segment.length), [2]);
  const restarted = [point(0), point(1, '2', { session: 'b', breakBefore: true }), point(2, '3', { session: 'b' })];
  assert.deepEqual(curveSegments(curveWindowPoints(restarted, window)).map(segment => segment.length), [1, 1]);
});

test('malformed timestamps at a viewport edge do not bridge an invalid sample', () => {
  const points = [point(0), { at: NaN, pnl: null }, point(2)];
  assert.deepEqual(curveWindowPoints(points, { from: 0, to: 2 * DAY }), points);
  assert.deepEqual(curveSegments(curveWindowPoints(points, { from: 0, to: 2 * DAY })).map(segment => segment.length), [1, 1]);
  assert.deepEqual(curveWindowPoints(points, { from: 0.25 * DAY, to: 1.75 * DAY }), []);
});
