import test from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness, profile, seedTestDashboard} from './ui-app-harness.mjs';

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;
const BASE = Date.UTC(2026, 8, 1);
const sample = (day, pnl = String(day), extra = {}) => ({at: BASE + day * DAY, bucket: day, pnl, session: 's', ...extra});
const history = (lastDay = 30) => ({generation: 'g', series: {
  H: Array.from({length: lastDay * 24 + 1}, (_, hour) => ({at: BASE + hour * HOUR, bucket: hour, pnl: String(hour / 24), session: 's'})),
  D: Array.from({length: lastDay + 1}, (_, day) => sample(day)),
}});

function chartPage(performance = history()) {
  const page = pageHarness();
  seedTestDashboard(page);
  page.ui.dashboard.generation = 'g';
  page.ui.performance = performance;
  page.ui.performanceAt = Date.now();
  page.element('#equity-chart').rect.width = 724;
  page.context.drawCurve();
  return page;
}

function fire(chart, type, fields = {}) {
  const event = {
    type, currentTarget: chart, pointerId: 1, pointerType: 'mouse', button: 0, isPrimary: true,
    clientX: 300, clientY: 60, preventDefault() { this.prevented = true; }, ...fields,
  };
  chart.handlers[type](event);
  return event;
}

const svgNodes = (page, className) => page.element('#equity-svg').children.filter(node => node.attributes.class === className);
const view = page => page.context.currentCurveWindow();

function assertNoWrites(page) {
  assert.equal(page.requests.some(request => request.options?.method && request.options.method !== 'GET'), false);
}

test('actual chart starts at the latest seven days and clips retained history at its edges', () => {
  const fullHistory = history();
  const page = chartPage(fullHistory);
  assert.equal(view(page).from, BASE + 23 * DAY);
  assert.equal(view(page).to, BASE + 30 * DAY);
  assert.equal(page.ui.performance, fullHistory);
  assert.equal(page.ui.performance.series.H.length, 721);
  assert.equal(page.element('#curve-period').textContent, '近 7 天');
  assert.equal(page.element('#curve-older').disabled, false);
  assert.equal(page.element('#curve-newer').disabled, true);
  assert.equal(page.element('#curve-latest').hidden, true);
  assert.equal(svgNodes(page, 'curve-line').length, 1);
  assert.equal(svgNodes(page, 'curve-line')[0].attributes['clip-path'], 'url(#curve-clip)');
  const clip = page.element('#equity-svg').children.find(node => node.tagName === 'clipPath');
  assert.equal(clip.children[0].attributes.width, '700');
  const oldChildren = page.element('#equity-svg').children;
  page.context.drawCurve();
  assert.notEqual(page.element('#equity-svg').children[0], oldChildren[0]);
  assert.equal(oldChildren.every(node => node.parentNode === null), true);
  assertNoWrites(page);
});

test('dragging actual chart pans backward, clamps at both boundaries and releases pointer capture', () => {
  const page = chartPage();
  const chart = page.element('#equity-chart');
  fire(chart, 'pointerdown');
  assert.equal(chart.hasPointerCapture(1), true);
  fire(chart, 'pointermove', {clientX: 500});
  assert.equal(view(page).to, BASE + 28 * DAY);
  assert.equal(view(page).from, BASE + 21 * DAY);
  assert.equal(chart.classList.contains('is-dragging'), true);
  assert.equal(page.element('#curve-latest').hidden, false);
  fire(chart, 'pointermove', {clientX: 10000});
  assert.equal(view(page).from, BASE);
  fire(chart, 'pointermove', {clientX: -10000});
  assert.equal(view(page).to, BASE + 30 * DAY);
  assert.equal(page.ui.curveEnd, null);
  fire(chart, 'pointerup', {clientX: -10000});
  assert.equal(page.ui.curveDrag, null);
  assert.equal(chart.hasPointerCapture(1), false);
  assert.equal(chart.classList.contains('is-dragging'), false);
  assertNoWrites(page);
});

test('vertical touch scrolling and non-primary presses do not pan the chart', () => {
  const page = chartPage();
  const chart = page.element('#equity-chart');
  fire(chart, 'pointerdown', {button: 2});
  assert.equal(page.ui.curveDrag, null);
  fire(chart, 'pointerdown', {isPrimary: false});
  assert.equal(page.ui.curveDrag, null);
  fire(chart, 'pointerdown', {pointerType: 'touch'});
  const motion = fire(chart, 'pointermove', {pointerType: 'touch', clientX: 308, clientY: 180});
  assert.equal(page.ui.curveEnd, null);
  assert.equal(page.ui.curveDrag.moved, false);
  assert.equal(motion.prevented, undefined);
  fire(chart, 'pointercancel', {pointerType: 'touch'});
  assert.equal(page.ui.curveDrag, null);
  assert.equal(chart.hasPointerCapture(1), false);
  assertNoWrites(page);
});

test('week navigation and keyboard controls remain bounded and do not trade', () => {
  const page = chartPage();
  const chart = page.element('#equity-chart');
  page.element('#curve-older').handlers.click();
  assert.equal(view(page).to, BASE + 23 * DAY);
  assert.equal(view(page).to - view(page).from, WEEK);
  assert.equal(fire(chart, 'keydown', {key: 'ArrowLeft'}).prevented, true);
  assert.equal(view(page).to, BASE + 22 * DAY);
  fire(chart, 'keydown', {key: 'PageUp'});
  assert.equal(view(page).to, BASE + 15 * DAY);
  fire(chart, 'keydown', {key: 'Home'});
  assert.equal(view(page).from, BASE);
  assert.equal(page.element('#curve-older').disabled, true);
  fire(chart, 'keydown', {key: 'PageDown'});
  assert.equal(view(page).to, BASE + 14 * DAY);
  page.element('#curve-newer').handlers.click();
  assert.equal(view(page).to, BASE + 21 * DAY);
  assertNoWrites(page);
});

test('refresh retains a historical anchor; End and latest restore following new samples', async () => {
  const page = chartPage();
  page.element('#curve-older').handlers.click();
  const olderEnd = page.ui.curveEnd;
  let next = history(31);
  page.context.fetch = async () => ({ok: true, headers: {get() {}}, text: async () => JSON.stringify(next)});
  await page.context.loadPerformance();
  assert.equal(page.ui.curveEnd, olderEnd);
  assert.equal(view(page).to, BASE + 23 * DAY);
  fire(page.element('#equity-chart'), 'keydown', {key: 'End'});
  assert.equal(page.ui.curveEnd, null);
  assert.equal(view(page).to, BASE + 31 * DAY);
  next = history(32);
  await page.context.loadPerformance();
  assert.equal(view(page).to, BASE + 32 * DAY);
  page.element('#curve-older').handlers.click();
  page.element('#curve-latest').handlers.click();
  assert.equal(page.ui.curveEnd, null);
  next = history(33);
  await page.context.loadPerformance();
  assert.equal(view(page).to, BASE + 33 * DAY);
});

test('H/D scale switches preserve the historical viewport and change only the sampled series', () => {
  const page = chartPage();
  page.element('#curve-older').handlers.click();
  const original = view(page);
  page.element('[data-curve-scale="D"]').handlers.click();
  assert.equal(page.ui.curveGranularity, 'D');
  assert.equal(view(page).from, original.from);
  assert.equal(view(page).to, original.to);
  assert.equal(page.element('[data-curve-scale="D"]').getAttribute('aria-pressed'), 'true');
  const dailyPointCount = svgNodes(page, 'curve-line')[0].attributes.points.split(' ').length;
  page.element('[data-curve-scale="H"]').handlers.click();
  assert.equal(page.ui.curveGranularity, 'H');
  assert.equal(view(page).from, original.from);
  assert.equal(view(page).to, original.to);
  assert.ok(svgNodes(page, 'curve-line')[0].attributes.points.split(' ').length > dailyPointCount);
  assertNoWrites(page);
});

test('changing mode clears the old curve and generation changes return to the latest history', async () => {
  const page = chartPage();
  page.element('#curve-older').handlers.click();
  page.element('#mode-toggle').handlers.click();
  assert.equal(page.ui.displayMode, 'LIVE');
  assert.equal(page.ui.curveEnd, null);
  assert.equal(page.ui.performance, null);
  assert.equal(svgNodes(page, 'curve-line').length, 0);
  assert.equal(view(page), null);
  const fresh = chartPage();
  fresh.element('#curve-older').handlers.click();
  const originalHistory = fresh.ui.performance;
  const originalEnd = fresh.ui.curveEnd;
  fresh.context.fetch = async () => ({ok: true, headers: {get() {}}, text: async () => JSON.stringify({...history(32), generation: 'new'})});
  await fresh.context.loadPerformance();
  assert.equal(fresh.ui.performance, originalHistory);
  assert.equal(fresh.ui.curveEnd, originalEnd);
  assert.equal(fresh.ui.performanceLoading, false);
  fresh.ui.dashboard.generation = 'new';
  await fresh.context.loadPerformance();
  assert.equal(fresh.ui.curveEnd, null);
  assert.equal(view(fresh).to, BASE + 32 * DAY);
  fresh.element('#curve-older').handlers.click();
  fresh.context.applyDashboardForTest({executionMode:'TEST', generation:'external-reset', details:'summary', preferences:{...profile}, strategy:{status:'PAUSED', initialCapital:'100', availableCash:'100'}, positions:[], positionCount:0, portfolio:{}, marketScan:{events:[], diagnostics:{availableCategories:[]}}});
  assert.equal(fresh.ui.curveEnd, null);
  assert.equal(fresh.ui.performance, null);
});

test('late performance from a replaced dashboard generation cannot repopulate its cleared curve', async () => {
  const page = chartPage();
  let resolveResponse;
  page.context.fetch = () => new Promise(resolve => { resolveResponse = resolve; });
  const oldRequest = page.context.loadPerformance();
  page.context.applyDashboardForTest({executionMode:'TEST', generation:'new', details:'summary', preferences:{...profile}, strategy:{status:'PAUSED', initialCapital:'100', availableCash:'100'}, positions:[], positionCount:0, portfolio:{}, marketScan:{events:[], diagnostics:{availableCategories:[]}}});
  assert.equal(page.ui.performance, null);
  resolveResponse({ok:true, headers:{get(){}}, text:async()=>JSON.stringify(history())});
  await oldRequest;
  assert.equal(page.ui.performance, null);
  assert.equal(svgNodes(page, 'curve-line').length, 0);
  assert.equal(page.ui.performanceLoading, false);
});

test('Shift and arrow keys announce an observed point without panning or selecting outside the viewport', () => {
  const page = chartPage();
  const chart = page.element('#equity-chart');
  const initial = view(page);
  const event = fire(chart, 'keydown', {key:'ArrowLeft', shiftKey:true});
  assert.equal(event.prevented, true);
  assert.equal(page.ui.chartIndex, 167);
  assert.equal(view(page).from, initial.from);
  assert.equal(view(page).to, initial.to);
  assert.match(page.element('#curve-status').textContent, /29\.96U/);
  assert.equal(svgNodes(page, 'curve-guide').length, 1);
  fire(chart, 'keydown', {key:'ArrowRight', shiftKey:true});
  assert.equal(page.ui.chartIndex, 168);
  fire(chart, 'keydown', {key:'ArrowRight', shiftKey:true});
  assert.equal(page.ui.chartIndex, 168);
  for (let index = 0; index < 170; index += 1) fire(chart, 'keydown', {key:'ArrowLeft', shiftKey:true});
  assert.equal(page.ui.chartIndex, 0);
  assert.match(page.element('#curve-status').textContent, /23\.00U/);
  fire(chart, 'keydown', {key:'ArrowLeft'});
  assert.equal(view(page).to, initial.to - DAY);
  assert.equal(page.ui.chartIndex, null);
  assertNoWrites(page);
});

test('actual SVG preserves an unknown interval as a gap instead of connecting its two sides', () => {
  const points = Array.from({length: 31}, (_, day) => sample(day, day === 27 ? null : String(day)));
  const page = chartPage({generation: 'g', points});
  const lines = svgNodes(page, 'curve-line');
  assert.equal(lines.length, 2);
  const xCoordinates = lines.map(line => line.attributes.points.split(' ').map(pair => Number(pair.split(',')[0])));
  assert.ok(Math.max(...xCoordinates[0]) < 412);
  assert.ok(Math.min(...xCoordinates[1]) > 412);
  assert.equal(points[27].pnl, null);
  assert.equal(page.ui.performance.points, points);
});

test('locked, stale and mismatched LIVE views cannot render cached performance history', () => {
  for (const fault of ['locked', 'stale', 'wrong-mode']) {
    const page = chartPage();
    page.ui.displayMode = 'LIVE';
    page.ui.dashboard = {...page.ui.dashboard, executionMode: 'LIVE', liveExecutionEnabled: true};
    page.ui.wallet = {configured:true, unlocked:true, enabled:true, status:'PAUSED', readiness:{ready:true}};
    page.ui.walletAt = Date.now();
    if (fault === 'locked') page.ui.wallet.unlocked = false;
    if (fault === 'stale') page.ui.walletAt -= 21000;
    if (fault === 'wrong-mode') page.ui.dashboard.executionMode = 'TEST';
    page.context.drawCurve();
    assert.equal(view(page), null);
    assert.equal(svgNodes(page, 'curve-line').length, 0);
    assert.equal(svgNodes(page, 'curve-point').length, 0);
    assert.match(page.element('#equity-svg').children[0].textContent, /历史暂不可读/);
    assert.equal(page.element('#curve-older').disabled, true);
    assertNoWrites(page);
  }
});

function verifiedLiveChart() {
  const page = chartPage();
  page.ui.displayMode = 'LIVE';
  page.ui.dashboard = {...page.ui.dashboard, executionMode:'LIVE', liveExecutionEnabled:true, positions:[], portfolio:{}};
  page.ui.wallet = {configured:true, unlocked:true, enabled:true, status:'PAUSED', readiness:{ready:true}};
  page.ui.walletAt = Date.now();
  page.context.drawCurve();
  assert.equal(svgNodes(page, 'curve-line').length, 1);
  return page;
}

test('actual wallet refresh immediately removes an already drawn curve on lock, readiness loss or read failure', async () => {
  for (const fault of ['locked', 'unready', 'read-failure']) {
    const page = verifiedLiveChart();
    const wallet = structuredClone(page.ui.wallet);
    if (fault === 'locked') { wallet.unlocked = false; wallet.status = 'LOCKED'; }
    if (fault === 'unready') wallet.readiness.ready = false;
    page.context.fetch = async () => {
      if (fault === 'read-failure') throw new Error('unavailable');
      return {ok:true, headers:{get(){}}, text:async()=>JSON.stringify(wallet)};
    };
    await page.context.loadLiveWallet();
    assert.equal(svgNodes(page, 'curve-line').length, 0);
    assert.equal(svgNodes(page, 'curve-point').length, 0);
    assert.equal(page.ui.curveAccountVisible, false);
    assert.match(page.element('#equity-svg').children[0].textContent, /历史暂不可读/);
  }
});

test('status tick clears an existing LIVE curve when its account expires and restores it only when verified again', () => {
  for (const fault of ['expired', 'locked', 'unready', 'read-error']) {
    const page = verifiedLiveChart();
    if (fault === 'expired') page.ui.walletAt -= 21000;
    if (fault === 'locked') page.ui.wallet.unlocked = false;
    if (fault === 'unready') page.ui.wallet.readiness.ready = false;
    if (fault === 'read-error') page.ui.walletError = 'not readable';
    page.context.renderStatus();
    assert.equal(svgNodes(page, 'curve-line').length, 0);
    assert.equal(page.ui.curveAccountVisible, false);
    page.ui.wallet = {configured:true, unlocked:true, enabled:true, status:'PAUSED', readiness:{ready:true}};
    page.ui.walletAt = Date.now();
    page.ui.walletError = null;
    page.context.renderStatus();
    assert.equal(svgNodes(page, 'curve-line').length, 1);
    assert.equal(page.ui.curveAccountVisible, true);
  }
});

test('long account explanations and settlement details stay inside collapsed disclosures', () => {
  const {html, element} = pageHarness();
  const portfolio = html.match(/<details id="portfolio-details"([^>]*)>([\s\S]*?)<\/details>/);
  assert.ok(portfolio);
  assert.doesNotMatch(portfolio[1], /\bopen\b/);
  assert.match(portfolio[2], /id="valuation-detail"/);
  assert.match(portfolio[2], /持仓按可成交买盘扣费估值/);
  assert.match(html, /<details class="module-status"><summary id="module-status-label">/);
  assert.match(html, /<details class="redemption-details"><summary>结算与赎回<\/summary>/);
  assert.equal(element('#portfolio-details').open, false);
  assert.ok(html.indexOf('id="valuation-note"') < html.indexOf('id="portfolio-details"'));
  assert.equal(html.includes('id="toggle-trade-records"'), false);
});

test('position summaries stay focused while strategy and financial details remain available on expansion', () => {
  const page = pageHarness();
  seedTestDashboard(page);
  page.ui.dashboard.positionCount = 1;
  page.context.renderPositions([{tokenId:'token', marketQuestion:'SAMPLE MARKET', direction:'Yes', averageBuyPrice:'0.02', currentSellPrice:'0.03', currentSellPriceStatus:'READY', quantity:'50', remainingCost:'1', unrealizedPnl:'0.5', estimatedUnrealizedPnl:'0.5', targetSellPrices:['0.04'], targetSellPrice:'0.04', stopLossThreshold:null, cycleStatus:'ACCUMULATING', cycleBudget:'2', cycleSpent:'1', progressPercent:25}]);
  const markup = page.element('#positions').innerHTML;
  const summary = markup.match(/<summary class="position-summary">([\s\S]*?)<\/summary>/)[1];
  assert.match(summary, /SAMPLE MARKET/);
  assert.match(summary, /均价 2.00¢/);
  assert.match(summary, /可卖价/);
  assert.doesNotMatch(summary, /目标卖价|冻结预算|市场进度|浮盈亏/);
  assert.match(markup, /剩余成本 <strong>1.00U/);
  assert.match(markup, /浮盈亏 <strong data-tone="positive">\+0.50U/);
  assert.match(markup, /<details class="strategy-detail"\s*><summary>策略详情<\/summary>/);
  assert.match(markup, /冻结预算|市场进度/);
});

test('position refresh preserves focus on the open nested strategy summary and market link', () => {
  for (const selector of ['.strategy-detail > summary', '.detail-market-link']) {
    const page = pageHarness();
    seedTestDashboard(page);
    page.ui.dashboard.positionCount = 1;
    const position = {tokenId:'token', marketQuestion:'SAMPLE MARKET', direction:'Yes', marketUrl:'https://example.com/market', averageBuyPrice:'0.02', currentSellPrice:'0.03', currentSellPriceStatus:'READY', quantity:'50', remainingCost:'1', unrealizedPnl:'0.5', estimatedUnrealizedPnl:'0.5', targetSellPrices:['0.04'], targetSellPrice:'0.04', stopLossThreshold:null, cycleStatus:'ACCUMULATING'};
    page.context.renderPositions([position]);
    const container = page.element('#positions');
    const originalRow = container.querySelector('details[data-token]');
    originalRow.open = true;
    originalRow.querySelector('.strategy-detail').open = true;
    const originalTarget = originalRow.querySelector(selector);
    originalTarget.focus();
    page.context.renderPositions([{...position, currentSellPrice:'0.031'}]);
    const refreshedRow = container.querySelector('details[data-token]');
    const refreshedTarget = refreshedRow.querySelector(selector);
    assert.notEqual(refreshedRow, originalRow);
    assert.notEqual(refreshedTarget, originalTarget);
    assert.equal(page.context.document.activeElement, refreshedTarget);
    assert.equal(refreshedTarget.focusOptions.preventScroll, true);
    assert.equal(refreshedRow.open, true);
    assert.equal(refreshedRow.querySelector('.strategy-detail').open, true);
    page.context.renderPositions([{...position, currentSellPrice:'0.031'}]);
    assert.equal(page.context.document.activeElement, refreshedTarget);
  }
});

test('market refresh restores focus to the expanded market link rather than its outer summary', () => {
  const page = pageHarness();
  seedTestDashboard(page);
  page.ui.activeTab = 'market';
  const candidate = {eventId:'event', eventTitle:'SAMPLE EVENT', marketUrl:'https://example.com/market', progressPercent:20, status:'NO_WINNER', representative:{marketQuestion:'SAMPLE EVENT', direction:'Yes', executableBuyPrice:'0.02', bestBid:'0.01'}, outcomes:[]};
  page.ui.events = [candidate];
  page.ui.displayCandidateCount = 1;
  page.context.renderCandidatesForTest();
  const container = page.element('#candidates');
  const originalRow = container.querySelector('details[data-event]');
  originalRow.open = true;
  const originalLink = originalRow.querySelector('.market-detail .market-link');
  originalLink.focus();
  page.ui.events = [{...candidate, progressPercent:21}];
  page.context.renderCandidatesForTest();
  const refreshedRow = container.querySelector('details[data-event]');
  const refreshedLink = refreshedRow.querySelector('.market-detail .market-link');
  assert.notEqual(refreshedLink, originalLink);
  assert.equal(page.context.document.activeElement, refreshedLink);
  assert.equal(refreshedLink.focusOptions.preventScroll, true);
  assert.equal(refreshedRow.open, true);
  page.context.renderCandidatesForTest();
  assert.equal(page.context.document.activeElement, refreshedLink);
});

test('opening the record tab reveals and loads records without requiring another expand action', () => {
  const page = pageHarness();
  seedTestDashboard(page);
  assert.equal(page.element('#trade-records-panel').hidden, true);
  page.element('#tab-records').handlers.click();
  assert.equal(page.ui.activeTab, 'records');
  assert.equal(page.ui.tradeRecordsExpanded, true);
  assert.equal(page.element('#trade-records-panel').hidden, false);
  assert.equal(page.element('#trade-records-content').hidden, false);
  assert.equal(page.element('#positions-panel').hidden, true);
  assert.match(page.element('#trade-records').innerHTML, /正在读取交易记录/);
  assert.equal(page.requests.some(request => request.path.includes('/trade-records')), true);
  page.element('#tab-positions').handlers.click();
  assert.equal(page.element('#trade-records-panel').hidden, true);
  assert.equal(page.element('#trade-records-content').hidden, true);
  assertNoWrites(page);
});
