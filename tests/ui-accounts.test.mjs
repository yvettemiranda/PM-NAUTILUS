import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness, profile, seedTestDashboard} from './ui-app-harness.mjs';

const reply = (body) => ({ok: true, headers: {get() { return null; }}, async text() { return JSON.stringify(body); }});
const url = (path) => new URL(path, 'https://localhost');
const accountOf = (path) => url(path).searchParams.get('account_id');
const snapshot = (status) => ({
  executionMode: 'TEST', accountId: 1, generation: 'test-generation', details: 'positions',
  capitalEditable: true, preferences: {...profile},
  strategy: {status, initialCapital: '100', availableCash: '99'},
  portfolio: {totalFunds: '99', availableCash: '99'}, positions: [],
  marketScan: {events: [], diagnostics: {availableCategories: []}},
});

test('dashboard has a separate backtest link and ignores the former TEST account browser selection', () => {
  const page = pageHarness({savedAccount: 10});
  assert.doesNotMatch(page.html, /test-account-select/);
  assert.match(page.html, /href="\/backtest\.html">历史回测/);
  assert.equal(page.ui.testAccountId, 1);
  assert.equal(accountOf(page.requests[0].path), '1');
  assert.equal(page.requests[0].options.method, undefined);
});

test('save, start, pause, sort and reset remain bound to the existing TEST account 1', async () => {
  const page = pageHarness({savedAccount: 5}); seedTestDashboard(page);
  const {ui, context, element} = page;
  ui.loading = false;
  let status = 'PAUSED';
  const writes = [];
  context.fetch = async (path, options) => {
    assert.equal(accountOf(path), '1');
    const endpoint = url(path).pathname.toLowerCase();
    if (options.method) {
      writes.push({path, body: options.body && JSON.parse(options.body)});
      if (endpoint.endsWith('/start')) status = 'RUNNING';
      if (endpoint.endsWith('/pause') || endpoint.endsWith('/reset')) status = 'PAUSED';
    }
    return reply(endpoint.endsWith('/performance') ?
      {accountId: 1, generation: 'test-generation', points: []} : snapshot(status));
  };
  await element('#config-form').handlers.submit({preventDefault() {}});
  assert.equal(ui.configDirty, false);
  await element('#run-toggle').handlers.click(); assert.equal(ui.strategyStatus, 'RUNNING');
  await element('#run-toggle').handlers.click(); assert.equal(ui.strategyStatus, 'PAUSED');
  element('#sort-toggle').dataset.nextSort = 'DESC';
  await element('#sort-toggle').handlers.click();
  await element('#reset-test').handlers.click();
  assert.deepEqual(writes.map(write => url(write.path).pathname), [
    '/api/test/preferences', '/api/test/start', '/api/test/pause', '/api/test/preferences', '/api/test/reset',
  ]);
  assert.equal(writes[0].body.initialCapital, 100);
  assert.equal(writes[3].body.candidateSortDirection, 'DESC');
  assert.deepEqual(writes[4].body, {confirmation: 'RESET TEST', finalConfirmation: 'RESET TEST AGAIN'});
  assert.equal(ui.dashboard.accountId, 1);
  assert.match(element('#message').textContent, /账户 1已彻底重置/);
});

test('switching between TEST and LIVE never writes or passes the TEST account to LIVE', () => {
  const page = pageHarness({savedAccount: 9}); seedTestDashboard(page);
  const {ui, element, requests} = page;
  element('#mode-toggle').handlers.click();
  assert.equal(ui.displayMode, 'LIVE');
  assert.equal(requests.slice(1).every(request => accountOf(request.path) === null), true);
  assert.ok(requests.some(request => request.path === '/api/live/wallet'));
  element('#mode-toggle').handlers.click();
  assert.equal(ui.displayMode, 'TEST');
  assert.equal(accountOf(requests.at(-1).path), '1');
  assert.equal(requests.every(request => !request.options.method), true);
});
