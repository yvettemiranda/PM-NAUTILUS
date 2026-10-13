// Execute the actual page handlers offline, with pending network requests.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const web = new URL('../src/pm_nautilus/web/', import.meta.url);
export const profile = JSON.parse(readFileSync(new URL('../config/strategy-profile.json', import.meta.url), 'utf8'));

export function pageHarness() {
  const nodes = new Map();
  function element(key) {
    if (nodes.has(key)) return nodes.get(key);
    const classes = new Set();
    const el = {
      id: key.startsWith('#') ? key.slice(1) : key,
      dataset: {}, value: '', textContent: '', innerHTML: '', hidden: false,
      checked: false, disabled: false, attributes: {}, handlers: {},
      classList: {
        add(value) { classes.add(value); }, remove(value) { classes.delete(value); },
        toggle() {}, contains(value) { return classes.has(value); },
      },
      setAttribute(k, v) { this.attributes[k] = v; },
      getAttribute(k) { return this.attributes[k]; }, removeAttribute(k) { delete this.attributes[k]; },
      addEventListener(k, fn) { this.handlers[k] = fn; },
      querySelector(k) { return element(`${key}/${k}`); },
      querySelectorAll() { return []; }, replaceChildren() {},
      scrollIntoView() {}, focus() {}, appendChild() {},
      getBoundingClientRect() { return {width: 240, height: 112}; },
    };
    nodes.set(key, el);
    return el;
  }
  const tabs = ['positions', 'market', 'records'].map(k => {
    const tab = element(`#tab-${k}`);
    tab.setAttribute('aria-controls', `panel-${k}`);
    return tab;
  });
  const context = {
    Date, Map, Set, Number, String, Boolean, Intl, Array, JSON, Math, AbortSignal, encodeURIComponent,
    fetch() { return new Promise(() => {}); },
    ResizeObserver: class { observe() {} },
    window: {
      location: {protocol: 'https:', hostname: 'localhost'},
      setTimeout() { return 1; }, clearTimeout() {}, intervals: [], setInterval(fn) { this.intervals.push(fn); }, confirm() { return true; },
    },
    document: {
      activeElement: null, hidden: false, handlers: {},
      addEventListener(k, fn) { this.handlers[k] = fn; }, querySelector: element,
      createElementNS(namespace, tag) { return element(`svg/${tag}`); },
      querySelectorAll(selector) {
        return selector === '.view-tabs [role="tab"]' ? tabs : [];
      },
    },
  };
  vm.createContext(context);
  const helpers = readFileSync(new URL('ui-state.js', web), 'utf8').replaceAll('export function', 'function');
  const app = readFileSync(new URL('app.js', web), 'utf8').replace(/^import .*\n/, '');
  vm.runInContext(`${helpers}\n${app}\nglobalThis.pageUi = ui; globalThis.renderCandidatesForTest = renderCandidates; globalThis.loadTradeRecordsForTest = loadTradeRecords; globalThis.renderCategoriesForTest = renderCategories; globalThis.applyDashboardForTest = applyDashboard;`, context);
  return {context, element, ui: context.pageUi};
}

export function seedTestDashboard(page) {
  const {ui, element} = page;
  ui.preferences = {...profile};
  ui.dashboard = {executionMode: 'TEST', preferences: ui.preferences, strategy: {status: 'PAUSED'}, capitalEditable: true};
  ui.dashboardAt = Date.now();
  ui.strategyStatus = 'PAUSED';
  for (const kind of ['binary', 'ternary', 'multi']) element(`#${kind}-market`).checked = true;
  element('#all-categories').checked = true;
  const fields = {
    'min-buy-price': profile.minBuyPriceCents, 'max-buy-price': profile.maxBuyPriceCents,
    'target-sell-increase': profile.targetSellPriceIncreaseCents,
    'target-sell-multiplier': profile.targetSellPriceMultiplier,
    'stop-loss-multiplier': profile.stopLossMultiplier,
    'min-market-duration': profile.minMarketDurationDays,
    'max-market-duration': profile.maxMarketDurationDays,
    'initial-capital': 100, 'order-amount': profile.orderAmount,
    'bid-ask-ratio': profile.minBidAskRatioPercent,
    'market-progress-filter': profile.maxMarketProgressPercent,
  };
  for (const [field, value] of Object.entries(fields)) element(`#${field}`).value = String(value);
  for (const button of ['run-toggle', 'reset-test', 'sort-toggle', 'save-config']) element(`#${button}`).disabled = false;
  element('#sort-toggle').dataset.nextSort = 'DESC';
}
