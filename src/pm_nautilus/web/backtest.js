const $ = (id) => document.getElementById(id);
const form = $("backtest-form");
const inputs = $("backtest-inputs");
const source = $("backtest-source");
const upload = $("backtest-upload");
const datasetMessage = $("backtest-data-message");
const status = $("backtest-status");
const chart = $("backtest-chart");
const API = "/api/backtest";
const SHANGHAI_OFFSET_MS = 8 * 60 * 60 * 1000;
const FILL_PREVIEW = 100;
const SVG = "http://www.w3.org/2000/svg";
const SESSION_JOB = "pm-backtest-job";

const defaults = {
  marketTypes: ["BINARY", "TERNARY"], allCategories: true, selectedCategoryIds: [],
  candidateSortDirection: "ASC", minBuyPriceCents: "0.1", maxBuyPriceCents: "99",
  targetSellPriceIncreaseCents: "1", targetSellPriceMultiplier: "1.5",
  stopLossEnabled: true, stopLossMultiplier: "0.4", minBidAskRatioPercent: 50,
  minMarketDurationDays: 1, maxMarketDurationDays: 30, maxMarketProgressPercent: 20,
  orderAmount: "1", includeIndependentMultiMarkets: false, includeAugmentedNegRisk: false,
  cyclePerMarket: false, buyBestLevelOnly: false,
};

const coverageLabels = {
  unknown: "覆盖情况未确认", partial: "数据覆盖不完整", scoped: "仅覆盖数据集声明的范围",
};
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 6 });
const money = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const time = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

let csrf = "";
let datasets = [];
let maxUploadBytes = 0;
let chunkBytes = 0;
let initializing = true;
let dataBusy = false;
let uploadController = null;
let copyBusy = false;
let starting = false;
let cancelling = false;
let running = null;
let jobSourceId = null;
let pollTimer = null;
let result = null;
let comparison = null;

function amount(value) {
  const n = numeric(value);
  return n === null ? "—" : `${money.format(n)} U`;
}

function numeric(value) {
  return (typeof value === "number" || typeof value === "string" && value.trim() !== "") &&
    Number.isFinite(Number(value)) ? Number(value) : null;
}

function count(value) {
  const n = numeric(value);
  return n === null ? "—" : number.format(n);
}

function dateText(ms) {
  return Number.isFinite(ms) ? time.format(ms + SHANGHAI_OFFSET_MS) : "—";
}

function localDate(ms) {
  return Number.isFinite(ms) ? new Date(ms + SHANGHAI_OFFSET_MS).toISOString().slice(0, -1) : "";
}

function shanghaiMs(value) {
  return Date.parse(`${value}+08:00`);
}

function setMessage(element, message, error = false) {
  element.textContent = message;
  element.dataset.tone = error ? "negative" : "";
}

async function api(path, { method = "GET", body, type, timeout = 15000, signal } = {}) {
  const headers = { "x-pm-csrf": csrf };
  if (type) headers["content-type"] = type;
  const deadline = AbortSignal.timeout(timeout);
  const response = await fetch(`${API}${path}`, {
    method, body, headers, signal: signal ? AbortSignal.any([deadline, signal]) : deadline,
  });
  csrf = response.headers.get("x-pm-csrf") || csrf;
  const raw = await response.text();
  let value = null;
  try { value = raw ? JSON.parse(raw) : null; } catch { /* Non-JSON proxy errors use HTTP status. */ }
  if (!response.ok) {
    const detail = value?.error || value?.message || value?.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map((item) => item.msg).filter(Boolean).join("；") : "";
    const error = new Error(message || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("服务器返回了无法识别的回测响应");
  return value;
}

function remember(id) {
  try {
    if (id) sessionStorage.setItem(SESSION_JOB, JSON.stringify({ id, sourceId: jobSourceId }));
    else sessionStorage.removeItem(SESSION_JOB);
  } catch { /* A private browsing session can still use the page. */ }
}

function restoreJob() {
  try { return JSON.parse(sessionStorage.getItem(SESSION_JOB)); } catch { return null; }
}

function selectedDataset() {
  return datasets.find((item) => item.id === source.value);
}

function coverageText(coverage) {
  return coverageLabels[coverage?.status ?? coverage] ?? coverageLabels.unknown;
}

function showDataset(resetDates) {
  const dataset = selectedDataset();
  if (resetDates) {
    for (const [id, key] of [["backtest-start", "startMs"], ["backtest-end", "endMs"]]) {
      const field = $(id);
      field.min = localDate(dataset?.startMs);
      field.max = localDate(dataset?.endMs);
      field.value = localDate(dataset?.[key]);
    }
  }
  $("backtest-source-scope").textContent = dataset
    ? `${coverageText(dataset.coverage)} · ${dateText(dataset.startMs)} 至 ${dateText(dataset.endMs)}（北京时间）`
    : "导入行情后可选择回测区间。";
  syncControls();
}

function syncControls() {
  const jobBusy = starting || running !== null || cancelling;
  inputs.disabled = initializing || jobBusy || dataBusy || !selectedDataset();
  $("backtest-copy-defaults").disabled = copyBusy;
  $("backtest-run").disabled = inputs.disabled || copyBusy;
  $("backtest-import").disabled = initializing || !maxUploadBytes || dataBusy || jobBusy;
  $("backtest-file").disabled = dataBusy;
  $("backtest-abort-upload").hidden = uploadController === null;
  $("backtest-abort-upload").disabled = Boolean(uploadController?.signal.aborted);
  $("backtest-cancel").hidden = !running;
  $("backtest-cancel").disabled = cancelling;
}

async function refreshDatasets(preferredId = source.value) {
  const value = await api("/datasets");
  datasets = Array.isArray(value.datasets) ? value.datasets : [];
  maxUploadBytes = Number(value.maxUploadBytes) || 0;
  chunkBytes = Number(value.uploadChunkBytes) || 768 * 1024;
  const options = datasets.map((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label || "未命名数据集";
    return option;
  });
  if (!options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "暂无数据集";
    options.push(option);
  }
  source.replaceChildren(...options);
  const next = datasets.find((item) => item.id === preferredId) ?? datasets[0];
  source.value = next?.id ?? "";
  $("backtest-upload-limit").textContent = maxUploadBytes
    ? `单个文件最多 ${number.format(maxUploadBytes / (1024 * 1024))} MiB。` : "";
  showDataset(true);
  if (!datasets.length) setMessage(datasetMessage, "还没有历史行情文件。");
  else setMessage(datasetMessage, `已导入 ${count(datasets.length)} 个数据集。`);
}

function applyPreferences(profile, initialCash) {
  for (const [key, fallback] of Object.entries(defaults)) {
    const value = profile?.[key] ?? fallback;
    const input = form.elements.namedItem(key);
    if (key === "marketTypes") {
      for (const option of input) option.checked = value.includes(option.value);
    } else if (key === "selectedCategoryIds") input.value = value.join(", ");
    else if (typeof fallback === "boolean") input.checked = Boolean(value);
    else input.value = String(value);
  }
  $("backtest-cash").value = initialCash ?? "100";
}

function collectPreferences() {
  const preferences = {};
  for (const [key, fallback] of Object.entries(defaults)) {
    const input = form.elements.namedItem(key);
    if (key === "marketTypes") {
      preferences[key] = Array.from(input).filter((option) => option.checked).map((option) => option.value);
    } else if (key === "selectedCategoryIds") {
      preferences[key] = [...new Set(input.value.trim().split(/[\s,，]+/).filter(Boolean))];
    } else if (typeof fallback === "boolean") preferences[key] = input.checked;
    else if (typeof fallback === "number") preferences[key] = Number(input.value);
    else preferences[key] = input.value;
  }
  if (!preferences.marketTypes.length) throw new Error("请至少选择一种市场类型。");
  if (!preferences.allCategories && !preferences.selectedCategoryIds.length) throw new Error("请输入至少一个栏目 ID。");
  if (Number(preferences.minBuyPriceCents) > Number(preferences.maxBuyPriceCents)) throw new Error("最低买价不能超过最高买价。");
  if (preferences.minMarketDurationDays > preferences.maxMarketDurationDays) throw new Error("最短市场时长不能超过最长市场时长。");
  return preferences;
}

async function readTestDefaults() {
  if (copyBusy) return;
  copyBusy = true;
  syncControls();
  try {
    const value = await api("/defaults");
    if (!value.preferences || numeric(value.initialCash) === null) throw new Error("服务器尚未提供 TEST 1 参数。");
    applyPreferences(value.preferences, value.initialCash);
    setMessage(status, "已读取当前 TEST 1 参数，可在本页修改后回测。");
  } catch (error) {
    setMessage(status, `读取 TEST 1 参数失败：${error.message} 可在这里手动设置。`, true);
  } finally {
    copyBusy = false;
    syncControls();
  }
}

function uploadStatus(received, total) {
  setMessage(datasetMessage, `正在导入… ${Math.floor(received / total * 100)}%`);
}

async function uploadDataset(file, signal) {
  const type = "application/x-ndjson";
  const { id } = await api("/uploads", { method: "POST" });
  const path = `/uploads/${encodeURIComponent(id)}`;
  try {
    let received = 0;
    while (received < file.size) {
      signal.throwIfAborted();
      const end = Math.min(received + chunkBytes, file.size);
      const response = await api(`${path}?offset=${received}`, {
        method: "PUT", body: file.slice(received, end), type, timeout: 60000, signal,
      });
      if (response.receivedBytes !== end) throw new Error("服务器收到的文件偏移与上传记录不一致，请重新导入。");
      received = end;
      uploadStatus(received, file.size);
    }
    signal.throwIfAborted();
    uploadController = null;
    setMessage(datasetMessage, "正在检查并保存行情包…");
    syncControls();
    return await api(`${path}/finish`, { method: "POST", timeout: 120000 });
  } catch (error) {
    try { await api(path, { method: "DELETE" }); } catch { /* Unfinished uploads expire on the server. */ }
    throw error;
  }
}

upload.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (initializing || dataBusy || starting || running) return;
  const file = $("backtest-file").files[0];
  if (!file || !file.size || file.size > maxUploadBytes) {
    setMessage(datasetMessage, "请选择非空且未超过大小限制的历史盘口文件。", true);
    return;
  }
  dataBusy = true;
  const controller = uploadController = new AbortController();
  syncControls();
  uploadStatus(0, file.size);
  try {
    const value = await uploadDataset(file, controller.signal);
    upload.reset();
    await refreshDatasets(value.dataset?.id);
    setMessage(datasetMessage, "导入完成。");
  } catch (error) {
    if (controller.signal.aborted) setMessage(datasetMessage, "已取消导入。");
    else setMessage(datasetMessage, `导入失败：${error.message}`, true);
  } finally {
    uploadController = null;
    dataBusy = false;
    syncControls();
  }
});

$("backtest-abort-upload").addEventListener("click", () => {
  uploadController?.abort();
  syncControls();
});

source.addEventListener("change", () => showDataset(true));
$("backtest-copy-defaults").addEventListener("click", readTestDefaults);

$("backtest-delete").addEventListener("click", async () => {
  const item = selectedDataset();
  if (!item || dataBusy || starting || running || !window.confirm(`删除回测数据集「${item.label}」？`)) return;
  dataBusy = true;
  syncControls();
  try {
    await api(`/datasets/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    await refreshDatasets();
    setMessage(datasetMessage, "已删除回测数据集。");
  } catch (error) {
    setMessage(datasetMessage, `删除失败：${error.message}`, true);
  } finally {
    dataBusy = false;
    syncControls();
  }
});

function schedulePoll() {
  clearTimeout(pollTimer);
  if (running) pollTimer = setTimeout(poll, 2000);
}

function handleJob(job) {
  if ((job.status === "running" || job.status === "completed" && !job.result) && job.id) {
    running = job.id;
    remember(job.id);
    setMessage(status, "正在回放历史盘口…");
    schedulePoll();
  } else {
    clearTimeout(pollTimer);
    running = null;
    if (job.status === "completed" && job.result) {
      remember(job.id);
      showResult(job.result, job.id, jobSourceId);
      setMessage(status, "回测完成。");
    } else {
      remember(null);
      setMessage(status, job.status === "cancelled" ? "回测已取消。" : job.error || "回测失败，服务器没有提供结果。", job.status !== "cancelled");
    }
  }
  syncControls();
}

async function poll() {
  const current = running;
  if (!current) return;
  try {
    const job = await api(`/jobs/${encodeURIComponent(current)}`);
    if (running === current) handleJob(job);
  } catch (error) {
    if (running !== current) return;
    if (error.status === 404 || error.status === 410) {
      handleJob({ status: "failed", error: "这次回测已过期，请重新运行。" });
      return;
    }
    setMessage(status, `暂时无法读取回测状态，正在重试：${error.message}`, true);
    schedulePoll();
  }
}

$("backtest-cancel").addEventListener("click", async () => {
  const current = running;
  if (!current || cancelling) return;
  cancelling = true;
  syncControls();
  try {
    const job = await api(`/jobs/${encodeURIComponent(current)}`, { method: "DELETE" });
    if (running === current) handleJob(job);
  } catch (error) {
    if (running === current) {
      setMessage(status, `无法确认取消结果：${error.message}`, true);
      schedulePoll();
    }
  } finally {
    cancelling = false;
    syncControls();
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const dataset = selectedDataset();
  if (initializing || !dataset || dataBusy || starting || running || copyBusy) return;
  const startMs = shanghaiMs($("backtest-start").value);
  const endMs = shanghaiMs($("backtest-end").value);
  const initialCash = $("backtest-cash").value;
  let payload;
  try {
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs ||
        startMs < dataset.startMs || endMs > dataset.endMs) throw new Error("请选择数据集范围内、结束晚于开始的时间段。");
    if (!/^[0-9]{1,7}(\.[0-9]{1,6})?$/.test(initialCash) || Number(initialCash) <= 0 || Number(initialCash) > 1000000) {
      throw new Error("起始资金须大于零且不超过 100 万 U，最多填写六位小数。");
    }
    const preferences = collectPreferences();
    if (Number(preferences.orderAmount) > Number(initialCash)) throw new Error("每轮预算不能超过起始资金。");
    payload = { sourceId: dataset.id, startMs, endMs, initialCash, preferences };
  } catch (error) {
    setMessage(status, error.message, true);
    return;
  }
  starting = true;
  syncControls();
  setMessage(status, "正在提交回测…");
  try {
    const job = await api("/jobs", { method: "POST", type: "application/json", body: JSON.stringify(payload) });
    jobSourceId = dataset.id;
    handleJob(job);
  } catch (error) {
    setMessage(status, `启动失败：${error.message}`, true);
  } finally {
    starting = false;
    syncControls();
  }
});

form.addEventListener("invalid", (event) => {
  const disclosure = event.target.closest("details");
  if (disclosure) disclosure.open = true;
}, true);

function addElement(parent, tag, content, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = content;
  parent.append(element);
  return element;
}

function showResult(value, id, sourceId) {
  const sameWindow = result?.id !== id && result?.value &&
    ["startMs", "endMs", "startingCash"].every((key) => result.value[key] === value[key]) &&
    sourceId && result.sourceId === sourceId;
  if (sameWindow) comparison = result.value;
  else if (result?.id !== id) comparison = null;
  result = { value, id, sourceId };
  $("backtest-results").hidden = false;
  $("backtest-result-context").textContent = `${value.source} · ${dateText(value.startMs)} 至 ${dateText(value.endMs)}（北京时间） · 起始 ${amount(value.startingCash)} · 空仓开始`;
  const summary = value.summary ?? {};
  const metrics = $("backtest-metrics");
  metrics.replaceChildren();
  for (const [label, content] of [
    ["期末净值", amount(summary.endingEquity)],
    ["期末盈亏", amount(summary.endingPnl)],
    ["已实现盈亏", amount(summary.realizedPnl)],
    ["期末现金", amount(summary.endingCash)],
    ["模拟成交", `${count(summary.fillCount)} 笔`],
    ["期末开放持仓", `${count(summary.openPositionCount)} 笔`],
  ]) {
    const measure = addElement(metrics, "div", "");
    addElement(measure, "dt", label);
    addElement(measure, "dd", content);
  }
  const cov = value.coverage ?? {};
  $("backtest-coverage-label").textContent =
    `${coverageText(cov)} · ${count(cov.marketCount)} 个市场 · ${count(cov.bookTokenCount)} 个结果有盘口 · ${count(cov.explicitGapCount)} 处已标记缺口 · ${count(cov.unpricedPointCount)} 个未估值采样点`;
  const notes = $("backtest-coverage-notes");
  notes.replaceChildren();
  if (cov.missingBookTokenCount) addElement(notes, "li", `${count(cov.missingBookTokenCount)} 个结果缺少盘口快照。`);
  if (cov.ignoredDeltaCount) addElement(notes, "li", `${count(cov.ignoredDeltaCount)} 条盘口更新无法用于重建。`);
  for (const note of cov.notes ?? []) addElement(notes, "li", note);
  const fills = Array.isArray(value.fills) ? value.fills : [];
  const rows = $("backtest-fill-rows");
  rows.replaceChildren();
  for (const item of fills.slice(0, FILL_PREVIEW)) {
    const row = addElement(rows, "tr", "");
    const price = numeric(item.price);
    const side = { BUY: "买", SELL: "卖" }[item.side] ?? item.side ?? "—";
    const market = item.question ?? item.marketId ?? item.eventId ?? "—";
    addElement(row, "td", dateText(item.atMs));
    addElement(row, "td", [market, item.outcome, item.kind === "SETTLEMENT" ? "结算" : side].filter(Boolean).join(" · "));
    addElement(row, "td", price === null ? "—" : `${number.format(price * 100)}¢`);
    addElement(row, "td", count(item.quantity));
    addElement(row, "td", numeric(item.fee) === null ? "—" : `${count(item.fee)} U`);
  }
  const fillNote = fills.length
    ? `显示 ${Math.min(fills.length, FILL_PREVIEW)} 笔，结果文件包含 ${count(fills.length)} 笔。`
    : "此区间没有模拟成交。";
  $("backtest-fill-note").textContent = summary.fillsTruncated
    ? `${fillNote} 总成交 ${count(summary.fillCount)} 笔，文件明细已截断。` : fillNote;
  drawChart(value, comparison);
}

function svg(tag, attributes, content) {
  const element = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (content !== undefined) element.textContent = content;
  chart.append(element);
}

function chartSeries(points, x, y, style) {
  let segment = [];
  function flush() {
    if (segment.length > 1) svg("polyline", { points: segment.map((point) => point.join(",")).join(" "), class: style });
    else if (segment.length === 1) svg("circle", { cx: segment[0][0], cy: segment[0][1], r: 3, class: style + " chart-point" });
    segment = [];
  }
  for (const point of points) {
    const pnl = numeric(point.pnl);
    if (point.breakBefore || pnl === null) flush();
    if (pnl !== null && Number.isFinite(point.atMs)) segment.push([x(point.atMs), y(pnl)]);
  }
  flush();
}

function drawChart(value, prior) {
  chart.replaceChildren();
  const legend = $("backtest-chart-legend");
  legend.replaceChildren();
  addElement(legend, "span", "本次回测", "chart-key-current");
  if (prior) addElement(legend, "span", "上次回测（同数据、时段及起始资金）", "chart-key-prior");
  const points = value.equity ?? [];
  const past = prior?.equity ?? [];
  const pnl = [...points, ...past].map((point) => numeric(point.pnl)).filter((value) => value !== null);
  const domain = pnl.reduce(([min, max], value) => [Math.min(value, min), Math.max(value, max)], [0, 0]);
  if (domain[0] === domain[1]) domain[1] = domain[0] + 1;
  const note = $("backtest-chart-note");
  note.textContent = pnl.length ? "曲线断开处表示覆盖缺口，或因缺少报价而无法估值。" : "此区间没有足够的报价绘制盈亏曲线。";
  $("backtest-sample-control").hidden = !points.length;
  const sample = $("backtest-sample");
  sample.max = Math.max(0, points.length - 1);
  sample.value = sample.max;
  showSample(points);
  if (!pnl.length) return;
  const x = (ms) => 74 + 702 * (ms - value.startMs) / (value.endMs - value.startMs);
  const y = (pnlValue) => 235 - 213 * (pnlValue - domain[0]) / (domain[1] - domain[0]);
  for (let i = 0; i <= 4; i++) {
    const tick = domain[0] + (domain[1] - domain[0]) * i / 4;
    svg("line", { x1: 74, x2: 776, y1: y(tick), y2: y(tick), class: "chart-gridline" });
    svg("text", { x: 68, y: y(tick) + 4, "text-anchor": "end" }, money.format(tick));
  }
  for (const [ms, endpoint] of [[value.startMs, "start"], [value.endMs, "end"]]) {
    svg("text", { x: x(ms), y: 258, "text-anchor": endpoint }, localDate(ms).slice(5, 16).replace("T", " "));
  }
  chartSeries(past, x, y, "chart-prior-line");
  chartSeries(points, x, y, "chart-current-line");
}

function showSample(points = result?.value.equity ?? []) {
  const item = points[Number($("backtest-sample").value)];
  $("backtest-sample-value").textContent = item
    ? `${dateText(item.atMs)} · 盈亏 ${amount(item.pnl)} · 净值 ${amount(item.total)} · 现金 ${amount(item.cash)}` : "";
}

$("backtest-sample").addEventListener("input", () => showSample());
$("backtest-download").addEventListener("click", () => {
  if (!result) return;
  const a = document.createElement("a");
  const url = URL.createObjectURL(new Blob([JSON.stringify(result.value, null, 2)], { type: "application/json" }));
  a.href = url;
  a.download = "pm-nautilus-backtest.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

async function initialize() {
  applyPreferences(defaults, "100");
  const saved = restoreJob();
  try { await refreshDatasets(); }
  catch (error) { setMessage(datasetMessage, `读取数据集失败：${error.message}`, true); }
  if (saved?.id) {
    try {
      const job = await api(`/jobs/${encodeURIComponent(saved.id)}`);
      jobSourceId = saved.sourceId;
      handleJob(job);
    }
    catch { remember(null); }
  }
  if (!running && !result) await readTestDefaults();
  initializing = false;
  syncControls();
}

void initialize();
