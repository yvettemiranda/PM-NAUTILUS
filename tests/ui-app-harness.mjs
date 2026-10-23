// Execute the actual page handlers offline, with pending network requests.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const web = new URL('../src/pm_nautilus/web/', import.meta.url);
export const profile = JSON.parse(readFileSync(new URL('../config/strategy-profile.json', import.meta.url), 'utf8'));

export function pageHarness() {
  const nodes = new Map();
  let svgId = 0;
  function matches(node, selector) {
    const parts = selector.trim().split(/\s*(>)\s*|\s+/).filter(Boolean);
    function simple(current, part) {
      if (!current) return false;
      const tag = part.match(/^[\w-]+/);
      if (tag && current.tagName !== tag[0]) return false;
      for (const [, name] of part.matchAll(/\.([\w-]+)/g)) if (!current.classList.contains(name)) return false;
      for (const [, name, value] of part.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
        if (name === 'open') { if (!current.open) return false; }
        else if (!(name in current.attributes) || (value !== undefined && current.attributes[name] !== value)) return false;
      }
      return true;
    }
    let current = node;
    if (!simple(current, parts.pop())) return false;
    while (parts.length) {
      const direct = parts.at(-1) === '>';
      if (direct) parts.pop();
      const part = parts.pop();
      current = current.parentNode;
      if (!direct) while (current && !simple(current, part)) current = current.parentNode;
      if (!simple(current, part)) return false;
    }
    return true;
  }
  function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
  function parseList(parent, markup) {
    parent.replaceChildren();
    const stack = [parent];
    for (const token of markup.matchAll(/<\/?([\w-]+)\b([^>]*)>/g)) {
      const [source, tag, attributes] = token;
      if (source.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      const child = element(`html/${tag}/${++svgId}`, true, tag);
      for (const [, name, value] of attributes.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) child.setAttribute(name, value ?? '');
      stack.at(-1).appendChild(child);
      if (!['input', 'br', 'img', 'hr', 'meta', 'link'].includes(tag) && !source.endsWith('/>')) stack.push(child);
    }
  }
  function element(key, fresh = false, tagName = 'div') {
    if (!fresh && nodes.has(key)) return nodes.get(key);
    const classes = new Set();
    const capturedPointers = new Set();
    let markup = '';
    const el = {
      id: key.startsWith('#') ? key.slice(1) : key,
      tagName, children: [], parentNode: null, open: false,
      dataset: {}, value: '', textContent: '', hidden: false,
      get innerHTML() { return markup; },
      set innerHTML(value) {
        markup = value;
        if (key === '#positions' || key === '#candidates') parseList(this, value);
      },
      checked: false, disabled: false, attributes: {}, handlers: {},
      rect: {left: 0, top: 0, width: 240, height: 200},
      classList: {
        add(value) { classes.add(value); }, remove(value) { classes.delete(value); },
        toggle(value, force) {
          const present = force === undefined ? !classes.has(value) : Boolean(force);
          if (present) classes.add(value); else classes.delete(value);
          return present;
        },
        contains(value) { return classes.has(value); },
      },
      setAttribute(k, v) {
        this.attributes[k] = String(v);
        if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(v);
        if (k === 'class') { classes.clear(); for (const name of String(v).split(/\s+/)) classes.add(name); }
        if (k === 'open') this.open = true;
      },
      getAttribute(k) { return this.attributes[k]; }, removeAttribute(k) { delete this.attributes[k]; },
      addEventListener(k, fn) { this.handlers[k] = fn; },
      querySelector(k) { return this.querySelectorAll(k)[0] || element(`${key}/${k}`); },
      querySelectorAll(k) { return descendants(this).filter(node => matches(node, k)); },
      closest(k) { let current = this; while (current && !matches(current, k)) current = current.parentNode; return current; },
      replaceChildren(...children) {
        for (const child of this.children) child.parentNode = null;
        this.children = [];
        for (const child of children) this.appendChild(child);
      },
      appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
      scrollIntoView() {}, focus(options) { this.focusOptions = options; context.document.activeElement = this; },
      setPointerCapture(id) { capturedPointers.add(id); },
      hasPointerCapture(id) { return capturedPointers.has(id); },
      releasePointerCapture(id) { capturedPointers.delete(id); },
      getBoundingClientRect() { return {...this.rect, right: this.rect.left + this.rect.width, bottom: this.rect.top + this.rect.height}; },
    };
    if (!fresh) nodes.set(key, el);
    return el;
  }
  const html = readFileSync(new URL('index.html', web), 'utf8');
  for (const [, tag, attributes, id] of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const el = element(`#${id}`);
    el.tagName = tag;
    el.hidden = /\shidden(?:\s|$)/.test(attributes);
    el.open = /\sopen(?:\s|$)/.test(attributes);
    for (const [, name, value] of attributes.matchAll(/([\w-]+)="([^"]*)"/g)) el.setAttribute(name, value);
  }
  const tabs = ['positions', 'market', 'records'].map(k => {
    const tab = element(`#tab-${k}`);
    return tab;
  });
  const scales = ['H', 'D'].map(scale => {
    const button = element(`[data-curve-scale="${scale}"]`);
    button.dataset.curveScale = scale;
    button.setAttribute('aria-pressed', String(scale === 'H'));
    return button;
  });
  const requests = [];
  const context = {
    Date, Map, Set, Number, String, Boolean, Intl, Array, JSON, Math, AbortSignal, encodeURIComponent,
    fetch(path, options) { requests.push({path, options}); return new Promise(() => {}); },
    ResizeObserver: class { observe() {} },
    window: {
      location: {protocol: 'https:', hostname: 'localhost'},
      setTimeout() { return 1; }, clearTimeout() {}, intervals: [], setInterval(fn) { this.intervals.push(fn); }, confirm() { return true; },
    },
    document: {
      activeElement: null, hidden: false, handlers: {},
      addEventListener(k, fn) { this.handlers[k] = fn; }, querySelector: element,
      createElementNS(namespace, tag) { return element(`svg/${tag}/${++svgId}`, true, tag); },
      querySelectorAll(selector) {
        return selector === '.view-tabs [role="tab"]' ? tabs : selector === '[data-curve-scale]' ? scales : [];
      },
    },
  };
  vm.createContext(context);
  const helpers = ['ui-state.js', 'curve-window.js'].map(file => readFileSync(new URL(file, web), 'utf8').replace(/export (function|const) /g, '$1 ')).join('\n');
  const app = readFileSync(new URL('app.js', web), 'utf8').replace(/^import .*\n/gm, '');
  vm.runInContext(`${helpers}\n${app}\nglobalThis.pageUi = ui; globalThis.renderCandidatesForTest = renderCandidates; globalThis.loadTradeRecordsForTest = loadTradeRecords; globalThis.renderCategoriesForTest = renderCategories; globalThis.applyDashboardForTest = applyDashboard;`, context);
  return {context, element, ui: context.pageUi, requests, html};
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
