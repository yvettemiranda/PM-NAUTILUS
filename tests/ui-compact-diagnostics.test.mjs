import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness, profile, seedTestDashboard} from './ui-app-harness.mjs';

const reply = body => ({ok: true, headers: {get() { return null; }}, async text() { return JSON.stringify(body); }});

for (const tab of ['positions', 'market', 'records']) test(`the ${tab} page explicitly requests compact diagnostics without changing its page`, async () => {
  const page = pageHarness({savedAccount: 2});
  seedTestDashboard(page);
  page.ui.loading = false;
  page.ui.activeTab = tab;
  page.ui.visibleCandidateCount = 40;
  page.ui.performanceAt = Date.now();
  const requests = [];
  page.context.fetch = async path => {
    requests.push(new URL(path, 'https://localhost'));
    return reply({
      executionMode: 'TEST', accountId: 2, generation: 'compact-page',
      details: tab === 'records' ? 'summary' : tab,
      preferences: profile, strategy: {status: 'PAUSED'}, capitalEditable: true,
      positions: [], positionCount: 0, portfolio: {},
      marketScan: {events: [], diagnostics: {availableCategories: []}},
    });
  };
  await page.context.loadDashboard();
  const request = requests.find(url => url.pathname === '/api/dashboard');
  assert.equal(request.searchParams.get('compact_diagnostics'), 'true');
  assert.equal(request.searchParams.get('account_id'), '2');
  assert.equal(request.searchParams.get('limit'), '40');
  assert.equal(request.searchParams.get('details'), tab === 'records' ? 'summary' : tab);
  assert.equal(page.ui.dashboard.accountId, 2);
  assert.equal(page.ui.dashboardError, null);
});
