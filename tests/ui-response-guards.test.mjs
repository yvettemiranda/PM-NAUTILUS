import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness, profile, seedTestDashboard} from './ui-app-harness.mjs';

const reply = (body, ok = true) => ({ok, headers: {get() { return null; }}, async text() { return JSON.stringify(body); }});
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function snapshot(accountId) {
  return {executionMode: 'TEST', accountId, generation: 'g', details: 'positions',
    preferences: {...profile}, strategy: {status: 'PAUSED', initialCapital: '100', availableCash: '100'},
    capitalEditable: true, portfolio: {}, positions: [], positionCount: 0,
    marketScan: {events: [], diagnostics: {availableCategories: []}}};
}
function pageAt(accountId = 2) {
  const page = pageHarness({savedAccount: accountId});
  seedTestDashboard(page);
  page.ui.dashboard = snapshot(accountId);
  page.ui.loading = false;
  page.ui.performanceAt = Date.now();
  page.context.renderRunControls();
  return page;
}
function assertBlocked(page) {
  assert.equal(page.ui.dashboard, null);
  assert.equal(page.ui.preferences, null);
  assert.match(page.ui.viewResponseError, /不兼容.*刷新/);
  assert.equal(page.element('#connection-warning').hidden, false);
  assert.match(page.element('#connection-warning').textContent, /刷新页面/);
  for (const control of ['run-toggle', 'reset-test', 'save-config', 'sort-toggle', 'config-toggle']) {
    assert.equal(page.element(`#${control}`).disabled, true, control);
  }
}

for (const mismatch of ['old-version', 'wrong-account', 'wrong-mode']) {
  test(`a ${mismatch} dashboard clears previously loaded account controls and fails closed`, async () => {
    const page = pageAt();
    const {ui, context, element} = page;
    ui.positionSnapshot = {mode: 'TEST', accountId: 2, generation: 'g', positions: [{tokenId: 'old'}]};
    ui.tradeRecords = [{id: 'old-record'}]; ui.recordCursor = 'g:10';
    ui.performance = {accountId: 2, generation: 'g', points: [{at: 1000, pnl: '1'}]};
    const body = snapshot(2);
    if (mismatch === 'old-version') delete body.accountId;
    else if (mismatch === 'wrong-account') body.accountId = 1;
    else body.executionMode = 'LIVE';
    const requests = [];
    context.fetch = async (path, options) => { requests.push({path, options}); return reply(body); };
    await context.loadDashboard();
    assertBlocked(page);
    assert.equal(ui.positionSnapshot, null); assert.equal(ui.performance, null);
    assert.equal(ui.recordCursor, null); assert.equal(ui.tradeRecords.length, 0);
    await element('#run-toggle').handlers.click();
    await element('#reset-test').handlers.click();
    await element('#sort-toggle').handlers.click();
    await element('#config-form').handlers.submit({preventDefault() {}});
    await context.loadDashboard(); await context.loadPerformance(); await context.loadTradeRecordsForTest();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.method, undefined);
  });
}

for (const [name, load] of [
  ['history', context => context.loadTradeRecordsForTest()],
  ['performance', context => context.loadPerformance()],
]) test(`an incompatible ${name} response disables old account operations too`, async () => {
  const page = pageAt();
  page.context.fetch = async () => reply({generation: 'g', records: [], points: []});
  await load(page.context);
  assertBlocked(page);
});

for (const [control, type] of [
  ['config-form', 'submit'], ['sort-toggle', 'click'], ['run-toggle', 'click'], ['reset-test', 'click'],
]) test(`an incompatible ${control} response cannot leave controls available for another write`, async () => {
  const page = pageAt();
  let requests = 0;
  page.context.fetch = async () => { requests++; return reply(snapshot(1)); };
  await page.element(`#${control}`).handlers[type]({preventDefault() {}});
  assertBlocked(page);
  assert.equal(page.ui.controlPending, false);
  await page.element('#run-toggle').handlers.click();
  await page.element('#config-form').handlers.submit({preventDefault() {}});
  assert.equal(requests, 1);
});

test('an incompatible late response cannot disable a different, newly loaded account', async () => {
  const page = pageAt();
  const {ui, context, element} = page;
  const requests = [];
  context.fetch = (path, options) => new Promise(resolve => requests.push({path, options, resolve}));
  const old = context.loadDashboard();
  element('#test-account-select').value = '3';
  element('#test-account-select').handlers.change({target: element('#test-account-select')});
  ui.performanceAt = Date.now();
  requests[1].resolve(reply(snapshot(3))); await tick();
  assert.equal(ui.dashboard.accountId, 3);
  requests[0].resolve(reply(snapshot(1))); await old;
  assert.equal(ui.dashboard.accountId, 3); assert.equal(ui.viewResponseError, null);
  assert.equal(element('#run-toggle').disabled, false);
});

test('an incompatible late TEST reply cannot disable a newly loaded LIVE view', async () => {
  const page = pageAt();
  const {ui, context, element} = page;
  const requests = [];
  context.fetch = (path, options) => new Promise(resolve => requests.push({path, options, resolve}));
  const old = context.loadDashboard();
  element('#mode-toggle').handlers.click();
  ui.performanceAt = Date.now();
  requests.find(request => request.path.includes('/api/dashboard') && request.path.includes('mode=LIVE'))
    .resolve(reply({...snapshot(2), executionMode: 'LIVE', liveExecutionEnabled: false}));
  await tick();
  requests[0].resolve(reply(snapshot(1))); await old;
  assert.equal(ui.displayMode, 'LIVE'); assert.equal(ui.dashboard.executionMode, 'LIVE');
  assert.equal(ui.viewResponseError, null); assert.equal(ui.dashboardError, null);
  assert.equal(element('#test-account-select').hidden, true);
});

test('a valid different account can be opened after rejecting an incompatible view', async () => {
  const page = pageAt();
  const {ui, context, element} = page;
  context.fetch = async () => reply(snapshot(1));
  await context.loadDashboard(); assertBlocked(page);
  context.fetch = async () => reply(snapshot(3));
  element('#test-account-select').value = '3';
  element('#test-account-select').handlers.change({target: element('#test-account-select')});
  await tick();
  assert.equal(ui.testAccountId, 3); assert.equal(ui.dashboard.accountId, 3);
  assert.equal(ui.viewResponseError, null); assert.equal(element('#run-toggle').disabled, false);
});

for (const mode of ['TEST', 'LIVE']) {
  test(`${mode} save preserves a later draft while recording exactly the submitted settings`, async () => {
    const page = pageAt();
    const {ui, context, element} = page;
    ui.displayMode = mode;
    ui.dashboard.executionMode = mode;
    ui.dashboard.liveExecutionEnabled = mode === 'LIVE';
    ui.wallet = {configured: true, unlocked: true, enabled: true, status: 'PAUSED', readiness: {ready: true}};
    ui.walletAt = Date.now();
    const requests = [];
    context.fetch = (path, options) => new Promise(resolve => requests.push({path, options, resolve}));
    element('#config-panel').hidden = false;
    element('#order-amount').value = '2'; element('#config-form').handlers.input();
    const pending = element('#config-form').handlers.submit({preventDefault() {}});
    assert.equal(JSON.parse(requests[0].options.body).orderAmount, 2);
    assert.equal(requests[0].path.includes('account_id=2'), mode === 'TEST');
    element('#order-amount').value = '3'; element('#config-form').handlers.input();
    const response = {...snapshot(2), executionMode: mode, preferences: {...profile, orderAmount: '2'}};
    requests[0].resolve(reply(response));
    await tick();
    const refresh = requests.find(request => request.path.includes('/api/dashboard'));
    assert.ok(refresh); refresh.resolve(reply(response));
    await pending;
    assert.equal(element('#order-amount').value, '3');
    assert.equal(ui.preferences.orderAmount, '2'); assert.equal(ui.dashboard.preferences.orderAmount, '2');
    assert.equal(ui.configDirty, true); assert.equal(ui.controlPending, false);
    assert.equal(element('#config-panel').hidden, false);
    assert.match(element('#message').textContent, /后续编辑尚未保存/);
    assert.equal(element('#save-config').disabled, false);
  });
}

test('a failed save keeps the later draft and does not claim it was stored', async () => {
  const page = pageAt();
  const {ui, context, element} = page;
  let resolveSave;
  context.fetch = () => new Promise(resolve => {resolveSave = resolve;});
  element('#order-amount').value = '2'; element('#config-form').handlers.input();
  const pending = element('#config-form').handlers.submit({preventDefault() {}});
  element('#order-amount').value = '3'; element('#config-form').handlers.input();
  resolveSave(reply({error: 'controlled save failure'}, false)); await pending;
  assert.equal(element('#order-amount').value, '3'); assert.equal(ui.configDirty, true);
  assert.equal(ui.preferences.orderAmount, profile.orderAmount);
  assert.match(element('#message').textContent, /controlled save failure/);
});
