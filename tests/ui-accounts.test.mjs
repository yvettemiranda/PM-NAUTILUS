import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness, profile, seedTestDashboard} from './ui-app-harness.mjs';

const reply = (body, ok = true) => ({ok, headers: {get() { return null; }}, async text() { return JSON.stringify(body); }});
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const url = path => new URL(path, 'https://localhost');
const accountOf = path => Number(url(path).searchParams.get('account_id'));
const snapshot = (accountId, extra = {}) => ({
  executionMode: 'TEST', accountId, generation: 'same-generation', details: 'positions',
  liveStrategyStatus: 'LOCKED', capitalEditable: true, preferences: {...profile, orderAmount: String(accountId)},
  strategy: {status: 'PAUSED', initialCapital: '100', availableCash: String(100 - accountId)},
  portfolio: {totalFunds: String(100 - accountId), availableCash: String(100 - accountId)},
  positions: [{tokenId: 'same-token', marketQuestion: `ACCOUNT_${accountId}_POSITION`, quantity: String(accountId), targetSellPrices: [], stopLossThreshold: null}],
  positionCount: 1, positionBooksReady: true,
  marketScan: {events: [], diagnostics: {availableCategories: []}}, ...extra,
});
const history = accountId => ({accountId, generation: 'same-generation', points: [
  {at: 1000, pnl: String(accountId), session: 's'}, {at: 2000, pnl: String(accountId + 1), session: 's'},
]});
function select(page, id) {
  const control = page.element('#test-account-select');
  control.value = String(id);
  control.handlers.change({target: control});
}
function pendingNetwork(page) {
  const requests = [];
  page.context.fetch = (path, options) => new Promise(resolve => requests.push({path, options, resolve}));
  return requests;
}

test('TEST has one numeric selector and each tab remembers only its own valid account', () => {
  const page = pageHarness();
  const options = page.html.match(/<select id="test-account-select"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.deepEqual([...options.matchAll(/<option value="(\d+)">(\d+)<\/option>/g)].map(match => match.slice(1)), Array.from({length: 10}, (_, i) => [String(i + 1), String(i + 1)]));
  const another = pageHarness();
  select(page, 10);
  assert.equal(page.ui.testAccountId, 10);
  assert.equal(page.sessionStore.get('pm-test-account'), '10');
  assert.equal(another.ui.testAccountId, 1);
  assert.equal(pageHarness({savedAccount: 10}).ui.testAccountId, 10);
  for (const invalid of [0, 11, -1, 2.5, 'broken']) assert.equal(pageHarness({savedAccount: invalid}).ui.testAccountId, 1);
  assert.equal(page.requests[0].path.includes('account_id=1'), true);
  assert.equal(pageHarness({savedAccount: 10}).requests[0].path.includes('account_id=10'), true);
});

test('switching TEST accounts clears all account data without controlling trading or changing wallet state', () => {
  const page = pageHarness(); seedTestDashboard(page);
  const {ui, context, element} = page;
  ui.performance = history(1); ui.performanceAt = Date.now();
  context.applyDashboardForTest(snapshot(1, {strategy: {status: 'RUNNING', initialCapital: '100', availableCash: '99'}}));
  ui.tradeRecords = [{id: 'account1'}]; ui.recordCursor = 'same-generation:100'; ui.recordHead = 'same-generation:200';
  ui.tradeRecordsLoaded = true; ui.tradeRecordsError = 'old account error'; ui.curveEnd = 1500;
  ui.events = [{eventTitle: 'ACCOUNT_1_MARKET'}]; ui.positionsExpanded = true;
  const wallet = {configured: true, unlocked: true}; ui.wallet = wallet; ui.walletEpoch = 7;
  const requests = pendingNetwork(page);
  select(page, 2);
  assert.equal(ui.testAccountId, 2); assert.equal(ui.dashboard, null); assert.equal(ui.preferences, null);
  assert.equal(ui.positionSnapshot, null); assert.equal(ui.performance, null); assert.equal(ui.curveEnd, null);
  assert.equal(ui.recordCursor, null); assert.equal(ui.recordHead, null); assert.equal(ui.tradeRecords.length, 0);
  assert.equal(ui.events.length, 0); assert.equal(ui.positionsExpanded, false); assert.equal(ui.tradeRecordsError, null);
  assert.equal(ui.wallet, wallet); assert.equal(ui.walletEpoch, 7);
  assert.equal(element('#positions').innerHTML.includes('ACCOUNT_1_POSITION'), false);
  assert.equal(requests.length, 1); assert.equal(url(requests[0].path).pathname, '/api/dashboard');
  assert.equal(accountOf(requests[0].path), 2); assert.equal(requests[0].options?.method, undefined);
  assert.equal(element('#settings-mode').textContent, 'TEST 账户 2 设置');
  assert.match(element('#run-toggle').getAttribute('aria-label'), /账户 2/);
});

test('drafts and pending writes block switching while background reads do not', async () => {
  const page = pageHarness(); seedTestDashboard(page);
  const {ui, element} = page;
  ui.configDirty = true;
  select(page, 2);
  assert.equal(ui.testAccountId, 1); assert.equal(element('#test-account-select').value, '1');
  assert.match(element('#message').textContent, /未保存/);
  ui.configDirty = false;
  const requests = pendingNetwork(page);
  const pending = element('#run-toggle').handlers.click();
  assert.equal(ui.controlPending, true); select(page, 2);
  assert.equal(ui.testAccountId, 1); assert.equal(requests.length, 1);
  requests[0].resolve(reply({error: 'try later'}, false)); await pending;
  select(page, 2);
  assert.equal(ui.testAccountId, 2); assert.equal(requests.length, 2);
});

test('rapid 1 to 2 to 1 switches reject old same-account dashboards and retain newer request ownership', async () => {
  const page = pageHarness(); seedTestDashboard(page);
  const {ui, context, element} = page;
  const requests = pendingNetwork(page);
  ui.loading = false;
  const old = context.loadDashboard();
  select(page, 2); select(page, 1);
  assert.deepEqual(requests.map(r => accountOf(r.path)), [1, 2, 1]);
  requests[0].resolve(reply(snapshot(1, {strategy: {status: 'RUNNING'}}))); await old;
  assert.equal(ui.dashboard, null); assert.equal(ui.loading, true);
  requests[1].resolve(reply(snapshot(2))); await tick();
  assert.equal(ui.dashboard, null); assert.equal(ui.loading, true);
  requests[2].resolve(reply(snapshot(1))); await tick();
  assert.equal(ui.dashboard.accountId, 1); assert.equal(ui.strategyStatus, 'PAUSED'); assert.equal(ui.loading, false);
  assert.match(element('#positions').innerHTML, /ACCOUNT_1_POSITION/);
});

test('a response labelled for another TEST account cannot populate the selected account', async () => {
  const page = pageHarness({savedAccount: 2}); seedTestDashboard(page);
  page.ui.dashboard = null; page.ui.loading = false;
  page.context.fetch = async () => reply(snapshot(1));
  await page.context.loadDashboard();
  assert.equal(page.ui.dashboard, null); assert.equal(page.ui.loading, false);
  assert.equal(page.element('#positions').innerHTML.includes('ACCOUNT_1_POSITION'), false);
});

for (const [control, type, endpoint] of [
  ['config-form', 'submit', '/api/test/preferences'], ['sort-toggle', 'click', '/api/test/preferences'],
  ['run-toggle', 'click', '/api/test/start'], ['reset-test', 'click', '/api/test/reset'],
]) test(`${control} binds account 2 in the request and ignores a late reply after the account changes`, async () => {
  const page = pageHarness({savedAccount: 2}); seedTestDashboard(page);
  const {ui, element} = page;
  const requests = pendingNetwork(page);
  const confirmations = []; page.context.window.confirm = message => { confirmations.push(message); return true; };
  const pending = element(`#${control}`).handlers[type]({preventDefault() {}});
  assert.equal(requests.length, 1); assert.equal(url(requests[0].path).pathname, endpoint); assert.equal(accountOf(requests[0].path), 2);
  if (control === 'reset-test') {
    assert.equal(confirmations.length, 2); assert.equal(confirmations.every(message => message.includes('TEST 账户 2')), true);
    assert.deepEqual(JSON.parse(requests[0].options.body), {confirmation: 'RESET TEST', finalConfirmation: 'RESET TEST AGAIN'});
  }
  const current = snapshot(3); ui.testAccountId = 3; ui.dashboard = current; ui.preferences = current.preferences; ui.strategyStatus = 'PAUSED';
  requests[0].resolve(reply(snapshot(2, {strategy: {status: 'RUNNING'}}))); await pending;
  assert.equal(ui.dashboard, current); assert.equal(ui.preferences, current.preferences); assert.equal(ui.strategyStatus, 'PAUSED');
  assert.equal(requests.length, 1); assert.equal(ui.controlPending, false);
});

test('account history cursors and late pagination cannot cross accounts or release newer reads', async () => {
  const page = pageHarness(); seedTestDashboard(page);
  const {ui, context} = page;
  ui.dashboard.generation = 'same-generation'; ui.recordCursor = 'same-generation:12'; ui.recordHead = 'same-generation:40';
  const requests = pendingNetwork(page);
  const old = context.loadTradeRecordsForTest({more: true});
  assert.equal(url(requests[0].path).searchParams.get('cursor'), 'same-generation:12');
  select(page, 2);
  context.applyDashboardForTest(snapshot(2));
  const current = context.loadTradeRecordsForTest();
  const currentRead = requests.find(r => r.path.includes('trade-records') && accountOf(r.path) === 2);
  assert.equal(url(currentRead.path).searchParams.has('cursor'), false); assert.equal(url(currentRead.path).searchParams.has('since'), false);
  requests[0].resolve(reply({accountId: 1, generation: 'same-generation', records: [{id: 'same-id', firstSeq: 1, quantity: '1'}], totalCount: 1})); await old;
  assert.equal(ui.tradeRecordsLoading, true); assert.equal(ui.tradeRecords.length, 0);
  currentRead.resolve(reply({accountId: 2, generation: 'same-generation', records: [{id: 'same-id', firstSeq: 1, quantity: '2'}], totalCount: 20, nextCursor: 'same-generation:1', head: 'same-generation:20'})); await current;
  assert.equal(ui.tradeRecordsLoading, false); assert.equal(ui.tradeRecords[0].quantity, '2');
  const more = context.loadTradeRecordsForTest({more: true});
  const moreRead = requests.at(-1);
  assert.equal(accountOf(moreRead.path), 2); assert.equal(url(moreRead.path).searchParams.get('cursor'), 'same-generation:1');
  moreRead.resolve(reply({accountId: 2, generation: 'same-generation', records: [{id: 'older', firstSeq: 0, quantity: '2'}], totalCount: 20})); await more;
  assert.deepEqual(Array.from(ui.tradeRecords, row => row.quantity), ['2', '2']);
});

test('successful save, start, pause, sort and reset all remain bound to account 5', async () => {
  const page = pageHarness({savedAccount: 5}); seedTestDashboard(page);
  const {ui, context, element} = page;
  ui.loading = false;
  let status = 'PAUSED';
  const writes = [];
  context.fetch = async (path, options) => {
    assert.equal(accountOf(path), 5);
    const endpoint = url(path).pathname.toLowerCase();
    if (options.method) {
      writes.push({path, body: options.body && JSON.parse(options.body)});
      if (endpoint.endsWith('/start')) status = 'RUNNING';
      if (endpoint.endsWith('/pause') || endpoint.endsWith('/reset')) status = 'PAUSED';
    }
    return reply(endpoint.endsWith('/performance') ? history(5) : snapshot(5, {strategy: {status, initialCapital: '100', availableCash: '95'}}));
  };
  await element('#config-form').handlers.submit({preventDefault() {}});
  assert.equal(ui.configDirty, false); assert.equal(ui.preferences.orderAmount, '5');
  await element('#run-toggle').handlers.click(); assert.equal(ui.strategyStatus, 'RUNNING');
  await element('#run-toggle').handlers.click(); assert.equal(ui.strategyStatus, 'PAUSED');
  element('#sort-toggle').dataset.nextSort = 'DESC';
  await element('#sort-toggle').handlers.click();
  await element('#reset-test').handlers.click();
  assert.equal(ui.strategyStatus, 'PAUSED'); assert.equal(ui.controlPending, false);
  assert.deepEqual(writes.map(write => url(write.path).pathname), ['/api/test/preferences', '/api/test/start', '/api/test/pause', '/api/test/preferences', '/api/test/reset']);
  assert.equal(writes[0].body.initialCapital, 100); assert.equal(writes[3].body.candidateSortDirection, 'DESC');
  assert.equal(ui.dashboard.accountId, 5); assert.equal(ui.testAccountId, 5);
  assert.match(element('#message').textContent, /账户 5已彻底重置/);
});

test('late curves cannot repopulate cleared history or release a newer account performance request', async () => {
  const page = pageHarness(); seedTestDashboard(page);
  const {ui, context} = page;
  ui.dashboard.generation = 'same-generation';
  const requests = pendingNetwork(page);
  const old = context.loadPerformance();
  select(page, 2); context.applyDashboardForTest(snapshot(2));
  const currentRead = requests.find(r => r.path.includes('performance') && accountOf(r.path) === 2);
  assert.ok(currentRead); assert.equal(ui.performanceLoading, true); assert.equal(ui.performance, null);
  requests[0].resolve(reply(history(1))); await old;
  assert.equal(ui.performanceLoading, true); assert.equal(ui.performance, null);
  currentRead.resolve(reply(history(2))); await tick();
  assert.equal(ui.performanceLoading, false); assert.equal(ui.performance.accountId, 2);
  assert.equal(context.curvePoints()[0].pnl, '2');
  ui.performance = history(1);
  assert.equal(context.curvePoints().length, 0); assert.equal(context.curveBounds(), null);
});

test('same-generation TEST holdings caches are still isolated by account identity', () => {
  const page = pageHarness(); seedTestDashboard(page);
  const {ui, context, element} = page;
  context.applyDashboardForTest(snapshot(1));
  assert.match(element('#positions').innerHTML, /ACCOUNT_1_POSITION/);
  ui.testAccountId = 2;
  context.applyDashboardForTest(snapshot(2, {details: 'summary', positions: []}));
  assert.equal(ui.positionSnapshot, null); assert.equal(element('#positions').innerHTML.includes('ACCOUNT_1_POSITION'), false);
  context.applyDashboardForTest(snapshot(2));
  assert.match(element('#positions').innerHTML, /ACCOUNT_2_POSITION/);
  assert.equal(element('#positions').innerHTML.includes('ACCOUNT_1_POSITION'), false);
});

test('LIVE requests retain their exact routes and never carry the TEST account selector', async () => {
  const page = pageHarness({savedAccount: 9}); seedTestDashboard(page);
  const {ui, context, element} = page;
  const requests = pendingNetwork(page);
  element('#mode-toggle').handlers.click();
  assert.equal(ui.displayMode, 'LIVE'); assert.equal(element('#test-account-select').hidden, true);
  assert.equal(requests.every(r => !url(r.path).searchParams.has('account_id')), true);
  assert.equal(requests.some(r => r.path === '/api/live/wallet'), true);
  assert.equal(requests.some(r => url(r.path).searchParams.get('mode') === 'LIVE'), true);
  const wallet = {configured: true, unlocked: true, enabled: true, status: 'PAUSED', readiness: {ready: true}};
  ui.wallet = wallet; ui.walletAt = Date.now(); ui.preferences = {...profile};
  ui.dashboard = {...snapshot(9), executionMode: 'LIVE', liveExecutionEnabled: true}; ui.dashboardAt = Date.now(); ui.strategyStatus = 'PAUSED';
  context.renderRunControls();
  const pending = element('#run-toggle').handlers.click();
  const control = requests.at(-1);
  assert.equal(control.path, '/api/live/start');
  control.resolve(reply({error: 'blocked'}, false)); await pending;
  select(page, 2); assert.equal(ui.testAccountId, 9); assert.equal(ui.wallet, wallet);
});

test('the actual selector switches 1 to 10, hides in LIVE and returns to TEST account 10', () => {
  const page = pageHarness(); seedTestDashboard(page);
  const {ui, element} = page;
  const requests = pendingNetwork(page);
  select(page, 10);
  assert.equal(ui.testAccountId, 10); assert.equal(element('#test-account-select').value, '10');
  assert.equal(element('#test-account-select').hidden, false); assert.equal(accountOf(requests.at(-1).path), 10);
  element('#mode-toggle').handlers.click();
  assert.equal(ui.displayMode, 'LIVE'); assert.equal(element('#test-account-select').hidden, true);
  assert.equal(requests.filter(r => url(r.path).searchParams.get('mode') === 'LIVE').every(r => !url(r.path).searchParams.has('account_id')), true);
  element('#mode-toggle').handlers.click();
  assert.equal(ui.displayMode, 'TEST'); assert.equal(ui.testAccountId, 10);
  assert.equal(element('#test-account-select').hidden, false); assert.equal(element('#test-account-select').value, '10');
  assert.equal(element('#settings-mode').textContent, 'TEST 账户 10 设置');
  assert.equal(accountOf(requests.at(-1).path), 10); assert.equal(page.sessionStore.get('pm-test-account'), '10');
  assert.equal(requests.every(r => !r.options.method), true);
});
