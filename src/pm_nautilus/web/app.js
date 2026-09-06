const $ = (selector) => document.querySelector(selector);

const ui = {
  dashboard: null,
  preferences: null,
  strategyStatus: "STOPPED",
  displayMode: "TEST",
  events: [],
  candidateCount: 0,
  displayCandidateCount: 0,
  staleCandidateCount: 0,
  visibleCandidateCount: 20,
  positionsExpanded: false,
  tradeRecordsExpanded: false,
  tradeRecordsLoaded: false,
  tradeRecordsLoading: false,
  tradeRecords: [],
  tradeRecordTotalCount: 0,
  tradeRecordsError: null,
  tradeRecordsLoadedAt: 0,
  configDirty: false,
  loading: false,
  reloadRequested: false,
  mutationVersion: 0,
  controlPending: false,
  messageTimer: null,
};

const POSITION_PREVIEW_LIMIT = 20;
const TRADE_RECORD_LIMIT = 20;
const TRADE_RECORD_REFRESH_MS = 3_000;
const CATEGORY_LABELS_ZH = {
  Politics: "政治",
  Sports: "体育",
  Crypto: "加密",
  Esports: "电竞",
  Iran: "伊朗",
  Finance: "财务",
  Geopolitics: "地缘政治",
  Tech: "科技",
  Culture: "文化",
  Economy: "经济",
  Weather: "天气",
  Mentions: "提及",
  Elections: "选举",
  Art: "艺术",
};
const REJECTION_LABELS_ZH = {
  RESULT_COUNT: "结果数",
  CATEGORY: "类别",
  GAME_START: "比赛已开始",
  DURATION_MISSING: "时长未知",
  DURATION_BELOW_MIN: "时长过短",
  DURATION_ABOVE_MAX: "时长过长",
  PROGRESS_MISSING: "进度未知",
  PROGRESS_BELOW_ZERO: "尚未开始",
  PROGRESS_ABOVE_MAX: "进度超限",
  BOOK_NOT_READY: "盘口未就绪",
  ASK_MISSING: "无卖盘",
  ASK_BELOW_MIN: "买价过低",
  ASK_ABOVE_MAX: "买价过高",
  BID_MISSING: "无买盘",
  BID_ASK_RATIO: "买卖盘比例不足",
  MIN_ORDER_SIZE: "最小下单量无效",
  TICK_SIZE: "最小价差无效",
  ORDER_BUDGET: "单笔金额不足",
};

let csrfToken = "";

async function api(path, options = {}) {
  path = path.replace("/api/test/", `/api/${ui.displayMode.toLowerCase()}/`);
  if (path.startsWith("/api/dashboard")) path += `&mode=${ui.displayMode}`;
  options.headers = { ...options.headers, "x-pm-csrf": csrfToken };
  const response = await fetch(path, {
    ...options,
    headers:
      options.body === undefined
        ? options.headers
        : { "content-type": "application/json", ...options.headers },
  });
  csrfToken = response.headers.get("x-pm-csrf") || csrfToken;
  const rawBody = await response.text();
  let body = {};
  if (rawBody) {
    try {
      body = JSON.parse(rawBody);
    } catch {
      body = { error: rawBody };
    }
  }
  if (!response.ok) {
    throw new Error(body.error || body.message || body.detail || `请求失败（${response.status}）`);
  }
  return body;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => {
    const entities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return entities[character];
  });
}

function showMessage(message, error = false) {
  const element = $("#message");
  element.textContent = message;
  element.className = error ? "error" : "success";
  if (ui.messageTimer !== null) window.clearTimeout(ui.messageTimer);
  ui.messageTimer = window.setTimeout(() => {
    element.textContent = "";
    element.className = "";
    ui.messageTimer = null;
  }, 4_500);
}

function formatMoney(value, signed = false) {
  if (value === null || value === undefined || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  const prefix = signed && amount > 0 ? "+" : "";
  return `${prefix}${amount.toFixed(2)}U`;
}

function formatCents(value) {
  if (value === null || value === undefined || value === "") return "—";
  const cents = Number(value) * 100;
  return Number.isFinite(cents) ? `${cents.toFixed(2)}¢` : "—";
}

function formatQuantity(value) {
  if (value === null || value === undefined || value === "") return "—";
  const quantity = Number(value);
  if (!Number.isFinite(quantity)) return "—";
  return quantity.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

function formatClock(value) {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(date)
    : "—";
}

function formatDate(value) {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(date)
    : "—";
}

function formatCount(value) {
  const count = Number(value);
  return Number.isFinite(count) ? Math.max(0, count).toLocaleString("zh-CN") : "0";
}

function setMoneyValue(selector, value, signed = false) {
  const element = $(selector);
  const amount = Number(value);
  element.textContent = formatMoney(value, signed);
  element.dataset.tone = amount > 0 ? "positive" : amount < 0 ? "negative" : "neutral";
}

function setButtonPending(button, pending, label = "处理中") {
  if (!button) return;
  if (pending) {
    button.dataset.idleLabel = button.textContent.trim();
    button.classList.add("is-pending");
    button.setAttribute("aria-busy", "true");
    button.textContent = label;
    button.disabled = true;
    return;
  }
  button.classList.remove("is-pending");
  button.removeAttribute("aria-busy");
  if (button.dataset.idleLabel) button.textContent = button.dataset.idleLabel;
  delete button.dataset.idleLabel;
  button.disabled = false;
}

function recordMutation() {
  ui.mutationVersion += 1;
  if (ui.loading) ui.reloadRequested = true;
}

function marketTitleMarkup(item) {
  const title = escapeHtml(item.marketQuestion || item.eventTitle || "未命名市场");
  return item.marketUrl
    ? `<a class="market-link" href="${escapeHtml(item.marketUrl)}" target="_blank" rel="noreferrer">${title}<span aria-hidden="true">↗</span></a>`
    : `<span class="market-link">${title}</span>`;
}

function progressMarkup(progressPercent, label = "市场进度") {
  const progress = Number(progressPercent);
  if (!Number.isFinite(progress)) {
    return '<div class="progress-track is-empty" aria-label="市场进度待更新"><span></span></div>';
  }
  const clamped = Math.min(100, Math.max(0, progress));
  return `<div class="progress-track" role="progressbar" aria-label="${escapeHtml(label)} ${clamped.toFixed(1)}%" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${clamped.toFixed(1)}"><span style="width:${clamped.toFixed(1)}%"></span></div>`;
}

function currentPositions(positions = []) {
  return positions.filter((position) => Number(position.quantity) > 0);
}

function renderPortfolio(portfolio, positions = []) {
  setMoneyValue("#total-funds", portfolio?.totalFunds);
  setMoneyValue("#realized-pnl", portfolio?.realizedPnl, true);
  setMoneyValue("#unrealized-pnl", portfolio?.unrealizedPnl, true);
  setMoneyValue("#position-value", portfolio?.positionValue);
  const positionCount = currentPositions(positions).length;
  const count = $("#portfolio-position-count");
  count.textContent = `${formatCount(positionCount)}单`;
  count.dataset.tone = "neutral";
}

function renderModeControl() {
  const modeToggle = $("#mode-toggle");
  const liveView = ui.displayMode === "LIVE";
  modeToggle.textContent = ui.displayMode;
  modeToggle.dataset.mode = ui.displayMode;
  modeToggle.setAttribute(
    "aria-label",
    liveView
      ? "当前 LIVE 视图，点击切换到 TEST"
      : "当前 TEST 模式，点击切换到 LIVE",
  );
  $("#live-lock").hidden = !liveView;
  $("#live-lock").textContent = ui.dashboard?.liveExecutionEnabled ? "LIVE 使用实际账户；START 开始真实交易，PAUSE 后退出与赎回继续。" : "LIVE 尚未在服务器配置并明确启用。";
}

function renderRunControls() {
  const running = ui.strategyStatus === "RUNNING";
  const paused = ui.strategyStatus === "PAUSED";
  const liveView = ui.displayMode === "LIVE";
  const runToggle = $("#run-toggle");
  if (!runToggle.classList.contains("is-pending")) {
    runToggle.textContent = running ? "PAUSE" : "START";
  }
  runToggle.disabled = ui.controlPending || (liveView && !ui.dashboard?.liveExecutionEnabled);
  runToggle.title = runToggle.disabled && liveView ? "LIVE 尚未由服务器启用" : "";
  runToggle.setAttribute(
    "aria-label",
    liveView && !ui.dashboard?.liveExecutionEnabled
      ? "LIVE 尚未开放，无法启动"
      : running
        ? `暂停 ${ui.displayMode} 自动买入`
        : `开始 ${ui.displayMode} 自动交易`,
  );

  const capital = $("#initial-capital");
  const capitalEditable = ui.dashboard?.capitalEditable === true;
  capital.disabled = !capitalEditable;
  capital.title = capitalEditable
    ? ""
    : liveView
      ? "实际余额由服务器账户查询更新，不能在页面修改"
    : running
      ? "修改总模拟资金前请先暂停TEST"
      : "总模拟资金仅能在暂停且没有交易记录时修改；如已有记录请先重置TEST";
  const reset = $("#reset-test");
  reset.disabled = !paused || ui.controlPending || liveView;
  $("#capital-label").textContent = liveView ? "实际账户余额（只读）" : "总模拟资金";
  $("#settings-mode").textContent = `${ui.displayMode} 设置`;
  reset.title = paused ? "" : "重置前必须先暂停TEST";
}

function renderPositions(positions = []) {
  const current = currentPositions(positions);
  if (current.length <= POSITION_PREVIEW_LIMIT) ui.positionsExpanded = false;
  const visible = ui.positionsExpanded
    ? current
    : current.slice(0, POSITION_PREVIEW_LIMIT);
  $("#position-count").textContent = `${formatCount(current.length)}单`;
  const controls = $("#position-list-controls");
  const toggle = $("#toggle-positions");
  controls.hidden = current.length <= POSITION_PREVIEW_LIMIT;
  $("#position-display-count").textContent =
    `当前显示 ${formatCount(visible.length)} / ${formatCount(current.length)}`;
  toggle.textContent = ui.positionsExpanded
    ? "收起至前20个"
    : `展开其余${formatCount(current.length - POSITION_PREVIEW_LIMIT)}个`;
  toggle.setAttribute("aria-expanded", String(ui.positionsExpanded));
  $("#positions").innerHTML = visible.length
    ? visible
        .map((position) => {
          const progress = Number(position.progressPercent);
          const progressText = Number.isFinite(progress) ? `${progress.toFixed(1)}%` : "待更新";
          const targets = Array.isArray(position.targetSellPrices)
            ? position.targetSellPrices
            : position.targetSellPrice === null
              ? []
              : [position.targetSellPrice];
          const targetLabel = targets.length > 1
            ? `${formatCents(targets[0])} 起 · ${targets.length}档`
            : formatCents(position.targetSellPrice);
          const eventTitle = position.eventTitle && position.eventTitle !== position.marketQuestion
            ? `<span class="event-title">${escapeHtml(position.eventTitle)}</span>`
            : "";
          const currentSellPrice = formatCurrentSellPrice(position);
          const cycleStatus = {
            ACCUMULATING: "仍可累计",
            EXITING: "退出阶段",
            STOP_ARMED: "止损确认中",
            STOP_EXITING: "止损退出中",
            LEGACY_CONFLICT: "旧仓冲突·只减仓",
          }[position.cycleStatus] || "周期状态待确认";
          const stopLossSummary = position.stopLossThreshold === null
            ? ""
            : `<span>止损线 <strong>${formatCents(position.stopLossThreshold)} (${escapeHtml(position.stopLossMultiplier)}×)</strong></span>`;
          return `<article class="position-row">
            <div class="row-heading">
              <div class="market-copy">${marketTitleMarkup(position)}${eventTitle}</div>
              <div class="outcome-badge"><small>买入结果</small><strong>${escapeHtml(position.direction || "—")}</strong></div>
            </div>
            <div class="quote-grid position-quotes">
              <div><span>实际买入</span><strong>${formatCents(position.averageBuyPrice)}</strong></div>
              <div><span>当前可卖</span><strong>${currentSellPrice}</strong></div>
              <div title="${escapeHtml(targets.map(formatCents).join("、"))}"><span>目标卖价</span><strong>${targetLabel}</strong></div>
              <div><span>持仓数量</span><strong>${formatQuantity(position.quantity)}</strong></div>
            </div>
            <div class="cycle-summary">
              <span>Event 周期 <strong>${escapeHtml(cycleStatus)}</strong></span>
              <span>冻结预算 <strong>${position.cycleBudget === null ? "—" : formatMoney(position.cycleBudget)}</strong></span>
              <span>本轮已用 <strong>${formatMoney(position.cycleSpent)}</strong></span>
              ${stopLossSummary}
            </div>
            <div class="progress-heading"><span>市场生命周期</span><strong>${progressText}</strong></div>
            ${progressMarkup(position.progressPercent, "市场生命周期")}
          </article>`;
        })
        .join("")
    : '<p class="empty-state">暂无已成交持仓</p>';
}

function renderTradeRecords() {
  const toggle = $("#trade-records-toggle");
  const content = $("#trade-records-content");
  const count = $("#trade-records-count");
  toggle.setAttribute("aria-expanded", String(ui.tradeRecordsExpanded));
  toggle.setAttribute(
    "aria-label",
    ui.tradeRecordsExpanded ? "收起交易记录" : "展开交易记录",
  );
  content.hidden = !ui.tradeRecordsExpanded;
  count.textContent = ui.tradeRecordsLoading && !ui.tradeRecordsLoaded
    ? "读取中"
    : ui.tradeRecordsLoaded
      ? `${formatCount(ui.tradeRecordTotalCount)}条`
      : "展开";
  if (!ui.tradeRecordsExpanded) return;

  if (ui.tradeRecordsLoading && !ui.tradeRecordsLoaded) {
    $("#trade-records").innerHTML = '<p class="empty-state">正在读取交易记录…</p>';
    return;
  }
  if (ui.tradeRecordsError !== null && !ui.tradeRecordsLoaded) {
    $("#trade-records").innerHTML = `<p class="empty-state trade-record-error">交易记录加载失败：${escapeHtml(ui.tradeRecordsError)}</p>`;
    return;
  }
  $("#trade-records").innerHTML = ui.tradeRecords.length
    ? ui.tradeRecords.map(tradeRecordMarkup).join("")
    : '<p class="empty-state">暂无交易记录</p>';
}

function tradeRecordMarkup(record) {
  const typeLabel = {
    OPEN: "开仓",
    ADD: "加仓",
    PARTIAL_CLOSE: "部分平仓",
    CLOSE: "已平仓",
    SETTLEMENT: "已结算",
  }[record.type] || "交易";
  const eventTitle = record.eventTitle && record.eventTitle !== record.marketQuestion
    ? `<span class="event-title">${escapeHtml(record.eventTitle)}</span>`
    : "";
  const direction = record.direction
    ? `<span class="trade-record-direction">${escapeHtml(record.direction)}</span>`
    : "";
  const price = record.price === null
    ? ""
    : `<span>价格 <strong>${formatCents(record.price)}</strong></span>`;
  const quantity = record.quantity === null
    ? ""
    : `<span>数量 <strong>${formatQuantity(record.quantity)}</strong></span>`;
  const amountLabel = record.type === "SETTLEMENT"
    ? "结算"
    : record.type === "OPEN" || record.type === "ADD"
      ? "投入"
      : "回款";
  const amount = `<span>${amountLabel} <strong>${formatMoney(record.amount)}</strong></span>`;
  const realizedPnl = record.realizedPnl === null
    ? ""
    : `<span>盈亏 <strong data-tone="${Number(record.realizedPnl) > 0 ? "positive" : Number(record.realizedPnl) < 0 ? "negative" : "neutral"}">${formatMoney(record.realizedPnl, true)}</strong></span>`;
  const settlementResult = record.type === "SETTLEMENT" && record.winningOutcome
    ? `<span>结果 <strong>${escapeHtml(record.winningOutcome)}</strong></span>`
    : "";
  return `<article class="trade-record-row">
    <div class="trade-record-main">
      <span class="trade-record-kind trade-record-kind-${escapeHtml(record.type.toLowerCase())}">${escapeHtml(typeLabel)}</span>
      <div class="trade-record-copy">${marketTitleMarkup(record)}${eventTitle}</div>
      <time datetime="${escapeHtml(record.occurredAt)}">${formatDate(record.occurredAt)}</time>
    </div>
    <div class="trade-record-meta">${direction}${price}${quantity}${amount}${realizedPnl}${settlementResult}</div>
  </article>`;
}

async function loadTradeRecords({ silent = false } = {}) {
  if (ui.tradeRecordsLoading) return;
  const mode = ui.displayMode;
  ui.tradeRecordsLoading = true;
  ui.tradeRecordsError = null;
  renderTradeRecords();
  try {
    const response = await api(`/api/test/trade-records?limit=${TRADE_RECORD_LIMIT}`);
    if (mode !== ui.displayMode) return;
    ui.tradeRecords = Array.isArray(response.records) ? response.records : [];
    ui.tradeRecordTotalCount = Number(response.totalCount) || 0;
    ui.tradeRecordsLoaded = true;
  } catch (error) {
    if (mode !== ui.displayMode) return;
    ui.tradeRecordsError = error.message;
    if (!silent) showMessage(`交易记录加载失败：${error.message}`, true);
  } finally {
    if (mode === ui.displayMode) {
      ui.tradeRecordsLoading = false;
      ui.tradeRecordsLoadedAt = Date.now();
      renderTradeRecords();
    }
  }
}

function formatCurrentSellPrice(position) {
  if (position.currentSellPriceStatus === "READY") {
    return formatCents(position.currentSellPrice);
  }
  return escapeHtml({
    NO_BID: "暂无买盘",
    NOT_READY: "行情未就绪",
    RECONNECTING: "行情重连中",
    DISCONNECTED: "行情已断开",
  }[position.currentSellPriceStatus] || "行情未就绪");
}

function renderCandidates() {
  const visible = ui.events;
  $("#candidate-count").textContent = ui.staleCandidateCount > 0
    ? `可交易${formatCount(ui.candidateCount)}个事件 · 待定${formatCount(ui.staleCandidateCount)}`
    : `${formatCount(ui.candidateCount)}个事件`;
  $("#display-count").textContent = `当前显示 ${formatCount(visible.length)} / ${formatCount(ui.displayCandidateCount)}`;
  const loadMore = $("#load-more");
  const allVisible = visible.length >= ui.displayCandidateCount;
  loadMore.disabled = allVisible;
  loadMore.textContent = allVisible ? "已全部显示" : "再显示20个";

  $("#candidates").innerHTML = visible.length
    ? visible
        .map((event) => {
          const candidate = event.winner || event.representative;
          const progress = Number(event.progressPercent);
          const progressText = Number.isFinite(progress) ? `${progress.toFixed(1)}%` : "待更新";
          const labels = Array.isArray(candidate.categoryLabels)
            ? candidate.categoryLabels.slice(0, 2)
            : candidate.category
              ? [candidate.category]
              : [];
          const category = labels
            .map((label) => `<span class="meta-pill">${escapeHtml(label)}</span>`)
            .join("");
          const statusText = {
            READY: event.locked ? "本轮已锁定" : "Winner已确定",
            INCOMPLETE: "等待兄弟盘口",
            NO_WINNER: event.locked ? "本轮已锁定·等待退出" : "暂无合格Winner",
            LEGACY_CONFLICT: "旧仓冲突·仅退出",
          }[event.status] || "等待评估";
          const outcomeChips = (event.outcomes || [])
            .slice(0, 4)
            .map((outcome) => `<span class="outcome-chip${outcome.isWinner ? " is-winner" : ""}">${escapeHtml(`${outcome.marketQuestion || "结果"} · ${outcome.direction || "—"}`)}</span>`)
            .join("");
          const remainingOutcomeCount = Math.max(0, Number(event.tokenCount || 0) - 4);
          const eventTitle = escapeHtml(event.eventTitle || candidate.eventTitle || "未命名事件");
          const eventLink = event.marketUrl
            ? `<a class="market-link" href="${escapeHtml(event.marketUrl)}" target="_blank" rel="noreferrer">${eventTitle}<span>↗</span></a>`
            : `<span class="market-link">${eventTitle}</span>`;
          const isReady = event.status === "READY" && event.winner;
          const buyLabel = isReady ? "Winner可买" : "参考买价";
          const sellLabel = isReady ? "Winner可卖" : "参考卖价";
          return `<article class="market-row event-row${isReady ? "" : " is-stale"}">
            <div class="row-heading">
              <div class="market-copy">
                ${eventLink}
                <div class="market-meta">${category}<span>${escapeHtml(event.resultCount)}元市场</span><span>${escapeHtml(event.eligibleTokenCount)}合格 / ${escapeHtml(event.tokenCount)} Token</span><span>${escapeHtml(event.marketCount)}市场</span><span class="event-status event-status-${escapeHtml(String(event.status).toLowerCase())}">${escapeHtml(statusText)}</span></div>
              </div>
              <span class="market-outcome">${event.winner ? `WIN ${escapeHtml(candidate.direction || "—")}` : "待定"}</span>
            </div>
            <div class="event-winner-title"><span>${event.winner ? "当前 Winner" : "当前代表"}</span><strong>${escapeHtml(candidate.marketQuestion || candidate.direction || "暂无")}</strong></div>
            <div class="quote-grid candidate-quotes">
              <div><span>${buyLabel}</span><strong>${formatCents(candidate.executableBuyPrice)}</strong></div>
              <div><span>${sellLabel}</span><strong>${formatCents(candidate.bestBid)}</strong></div>
              <div class="market-time"><span>开始</span><strong>${formatDate(event.openedAt)}</strong></div>
              <div class="market-time"><span>结束</span><strong>${formatDate(event.endsAt)}</strong></div>
            </div>
            <div class="event-outcomes">${outcomeChips}${remainingOutcomeCount > 0 ? `<span class="outcome-chip">+${remainingOutcomeCount}</span>` : ""}</div>
            <div class="progress-heading"><span>市场生命周期</span><strong>${progressText}</strong></div>
            ${progressMarkup(event.progressPercent, "市场生命周期")}
          </article>`;
        })
        .join("")
    : '<p class="empty-state">当前没有符合配置的事件</p>';
}

function renderScanStatus(status) {
  const element = $("#scan-refresh-state");
  const diagnostics = status?.diagnostics;
  element.classList.toggle("is-refreshing", status?.scanning === true);
  element.classList.toggle("is-error", Boolean(status?.lastError));
  const rejectionSummary = Object.entries(diagnostics?.rejectionCounts ?? {})
    .filter(([, count]) => Number(count) > 0)
    .sort((left, right) => Number(right[1]) - Number(left[1]))
    .slice(0, 4)
    .map(([reason, count]) => `${REJECTION_LABELS_ZH[reason] || reason} ${formatCount(count)}`)
    .join("、");
  element.title = rejectionSummary ? `主要排除原因：${rejectionSummary}` : "";

  if (status?.scanning) {
    if (diagnostics?.phase === "ORDER_BOOKS") {
      element.textContent = `盘口 ${formatCount(diagnostics.orderBookCount)} / ${formatCount(diagnostics.orderBookTargetTokenCount)} · 保留 ${formatCount(status.displayCandidateCount)}`;
    } else {
      element.textContent = `扫描事件 ${formatCount(diagnostics?.eventCount)} · 保留事件 ${formatCount(status.displayEventCount ?? status.displayCandidateCount)}`;
    }
    return;
  }

  if (status?.lastError) {
    element.textContent = `扫描失败，已保留上次结果 · ${formatClock(diagnostics?.completedAt)}`;
    element.title = status.lastError;
    return;
  }

  if (status?.lastScanAt) {
    element.textContent = `扫描完成 · 监控 ${formatCount(status.tokenCount ?? diagnostics?.monitoredTokenCount)} Token · 可交易 ${formatCount(status.eventCount ?? status.candidateCount)} 个事件`;
    return;
  }
  element.textContent = "等待首次扫描";
}

function availableCategories(preferences) {
  const scanned = ui.dashboard?.marketScan?.diagnostics?.availableCategories ?? [];
  const categories = new Map();
  for (const item of scanned) {
    const category = typeof item === "string" ? { id: item, label: item } : item;
    if (category?.id) categories.set(category.id, { id: category.id, label: category.label || category.id });
  }
  return Array.from(categories.values());
}

function categoryDisplayLabel(category) {
  return CATEGORY_LABELS_ZH[category.label] || category.label;
}

function renderCategories(preferences) {
  const categories = availableCategories(preferences);
  const selectedIds = preferences.selectedCategoryIds ?? preferences.selectedCategories ?? [];
  const all = preferences.allCategories;
  $("#all-categories").checked = all;
  $("#category-options").innerHTML = categories
    .map((category) => `<label class="category-chip">
      <input type="checkbox" data-category-id="${escapeHtml(category.id)}" ${all || selectedIds.includes(category.id) ? "checked" : ""} />
      <span>${escapeHtml(categoryDisplayLabel(category))}</span>
    </label>`)
    .join("");
  $("#category-note").textContent = categories.length
    ? all
      ? `已全选 ${categories.length} 个首页栏目 · 新栏目自动纳入`
      : `已选 ${selectedIds.length}/${categories.length} 个首页栏目`
    : "正在同步首页栏目…";
}

function isTenthCent(value) {
  return Number.isFinite(value) && Number.isInteger(value * 10);
}

function displayConfigNumber(value, fallback) {
  return Number.isFinite(value) ? String(value) : fallback;
}

function numberFromInput(selector) {
  const rawValue = $(selector).value.trim();
  return rawValue === "" ? Number.NaN : Number(rawValue);
}

function renderTargetSellFormula() {
  const increase = numberFromInput("#target-sell-increase");
  const multiplier = numberFromInput("#target-sell-multiplier");
  $("#target-sell-formula").textContent =
    `卖价=min(99¢,tick↑max(买价+${displayConfigNumber(increase, "—")}¢,买价×${displayConfigNumber(multiplier, "—")}))`;
}

function renderStopLossControl() {
  const enabled = $("#stop-loss-enabled").checked;
  $("#stop-loss-multiplier").disabled = !enabled;
  $(".stop-loss-multiplier-field").setAttribute(
    "aria-disabled",
    String(!enabled),
  );
}

function renderPreferences(preferences, strategy, force = false) {
  if (ui.configDirty && !force) return;
  $("#binary-market").checked = preferences.marketTypes.includes("BINARY");
  $("#ternary-market").checked = preferences.marketTypes.includes("TERNARY");
  $("#multi-market").checked = preferences.marketTypes.includes("MULTI");
  $("#min-buy-price").value = String(preferences.minBuyPriceCents);
  $("#max-buy-price").value = String(preferences.maxBuyPriceCents);
  $("#target-sell-increase").value = String(preferences.targetSellPriceIncreaseCents);
  $("#target-sell-multiplier").value = String(preferences.targetSellPriceMultiplier);
  $("#stop-loss-enabled").checked = preferences.stopLossEnabled === true;
  $("#stop-loss-multiplier").value = String(preferences.stopLossMultiplier);
  renderStopLossControl();
  renderTargetSellFormula();
  $("#bid-ask-ratio").value = String(preferences.minBidAskRatioPercent);
  $("#bid-ask-ratio-value").textContent = String(preferences.minBidAskRatioPercent);
  $("#bid-ask-ratio").setAttribute("aria-valuetext", `至少${preferences.minBidAskRatioPercent}%`);
  $("#market-progress-filter").value = String(preferences.maxMarketProgressPercent);
  $("#market-progress-value").textContent = String(preferences.maxMarketProgressPercent);
  $("#market-progress-filter").setAttribute("aria-valuetext", `不超过${preferences.maxMarketProgressPercent}%`);
  $("#min-market-duration").value = String(preferences.minMarketDurationDays);
  $("#max-market-duration").value = String(preferences.maxMarketDurationDays);
  $("#min-duration-value").textContent = String(preferences.minMarketDurationDays);
  $("#max-duration-value").textContent = String(preferences.maxMarketDurationDays);
  $("#initial-capital").value = (ui.displayMode === "LIVE" ? strategy.availableCash : strategy.initialCapital) === null ? "" : Number(ui.displayMode === "LIVE" ? strategy.availableCash : strategy.initialCapital).toFixed(2);
  $("#order-amount").value = Number(preferences.orderAmount).toFixed(2);
  renderCategories(preferences);
  updateSortToggle(preferences.candidateSortDirection);
  renderRunControls();
}

function updateSortToggle(direction) {
  const button = $("#sort-toggle");
  const ascending = direction === "ASC";
  button.dataset.nextSort = ascending ? "DESC" : "ASC";
  button.querySelector("strong").textContent = ascending ? "↑" : "↓";
  button.setAttribute(
    "aria-label",
    ascending
      ? "当前按市场进度正序，点击切换为倒序"
      : "当前按市场进度倒序，点击切换为正序",
  );
}

function applyDashboard(dashboard) {
  ui.dashboard = dashboard;
  renderModeControl();
  $("#cash-note").textContent = `可用 ${formatMoney(dashboard.portfolio.availableCash)} · 已占用 ${formatMoney(dashboard.portfolio.reservedCash)} · 待赎回 ${formatMoney(dashboard.portfolio.pendingRedemption)}`;
  $("#redemption-status").textContent = (dashboard.redemptions || []).map(c => `${c.condition_id.slice(0, 10)}… ${c.state}${c.error ? `：${c.error}` : ""}`).join(" · ");
  $("#redemption-status").hidden = !$("#redemption-status").textContent;
  ui.preferences = dashboard.preferences;
  ui.strategyStatus = dashboard.strategy.status;
  ui.events = dashboard.marketScan.events ?? [];
  ui.candidateCount = dashboard.marketScan.eventCount ?? dashboard.marketScan.candidateCount ?? ui.events.length;
  ui.displayCandidateCount = dashboard.marketScan.displayEventCount ?? dashboard.marketScan.displayCandidateCount ?? ui.events.length;
  ui.staleCandidateCount = dashboard.marketScan.pendingEventCount ?? dashboard.marketScan.staleCandidateCount ?? 0;
  renderRunControls();
  renderPortfolio(dashboard.portfolio, dashboard.positions);
  renderPositions(dashboard.positions);
  renderCandidates();
  renderScanStatus(dashboard.marketScan);
  renderPreferences(dashboard.preferences, dashboard.strategy);
  if (
    ui.tradeRecordsExpanded &&
    Date.now() - ui.tradeRecordsLoadedAt >= TRADE_RECORD_REFRESH_MS
  ) {
    void loadTradeRecords({ silent: true });
  }
}

async function loadDashboard({ silent = false } = {}) {
  if (ui.loading) {
    ui.reloadRequested = true;
    return;
  }
  ui.loading = true;
  const mutationVersion = ui.mutationVersion;
  try {
    const dashboard = await api(`/api/dashboard?limit=${ui.visibleCandidateCount}`);
    if (mutationVersion === ui.mutationVersion) applyDashboard(dashboard);
  } catch (error) {
    if (!silent) showMessage(`数据刷新失败：${error.message}`, true);
  } finally {
    ui.loading = false;
    if (ui.reloadRequested) {
      ui.reloadRequested = false;
      void loadDashboard({ silent: true });
    }
  }
}

function setConfigOpen(open) {
  $("#config-panel").hidden = !open;
  $("#config-toggle").setAttribute("aria-expanded", String(open));
}

function selectedCategoriesFromForm() {
  return Array.from(document.querySelectorAll("[data-category-id]:checked")).map(
    (input) => input.dataset.categoryId,
  );
}

function collectConfigPayload() {
  const marketTypes = [];
  if ($("#binary-market").checked) marketTypes.push("BINARY");
  if ($("#ternary-market").checked) marketTypes.push("TERNARY");
  if ($("#multi-market").checked) marketTypes.push("MULTI");
  if (marketTypes.length === 0) throw new Error("请至少选择一种市场类型");

  const allCategories = $("#all-categories").checked;
  const selectedCategoryIds = selectedCategoriesFromForm();
  if (!allCategories && selectedCategoryIds.length === 0) {
    throw new Error("请选择至少一个市场类别，或选择全部符合条件");
  }

  const minBuyPriceCents = numberFromInput("#min-buy-price");
  const maxBuyPriceCents = numberFromInput("#max-buy-price");
  const targetSellPriceIncreaseCents = numberFromInput("#target-sell-increase");
  const targetSellPriceMultiplier = numberFromInput("#target-sell-multiplier");
  const stopLossEnabled = $("#stop-loss-enabled").checked;
  const stopLossMultiplier = numberFromInput("#stop-loss-multiplier");
  const minMarketDurationDays = Number($("#min-market-duration").value);
  const maxMarketDurationDays = Number($("#max-market-duration").value);
  const initialCapital = Number($("#initial-capital").value);
  const orderAmount = Number($("#order-amount").value);
  const minBidAskRatioPercent = Number($("#bid-ask-ratio").value);
  const maxMarketProgressPercent = Number($("#market-progress-filter").value);
  if (!isTenthCent(minBuyPriceCents) || minBuyPriceCents < 0.1 || minBuyPriceCents > 99) {
    throw new Error("最低买入价必须是0.1至99之间、按0.1递增的美分价格");
  }
  if (!isTenthCent(maxBuyPriceCents) || maxBuyPriceCents < 0.1 || maxBuyPriceCents > 99) {
    throw new Error("最高买入价必须是0.1至99之间、按0.1递增的美分价格");
  }
  if (minBuyPriceCents > maxBuyPriceCents) {
    throw new Error("最低买入价不能超过最高买入价");
  }
  if (!Number.isFinite(targetSellPriceIncreaseCents) || targetSellPriceIncreaseCents < 0 || targetSellPriceIncreaseCents > 99) {
    throw new Error("目标卖价加价参数必须是0至99之间的美分价格");
  }
  if (!Number.isFinite(targetSellPriceMultiplier) || targetSellPriceMultiplier < 0) {
    throw new Error("目标卖价倍数参数必须是大于等于0的数字");
  }
  if (!Number.isFinite(stopLossMultiplier) || stopLossMultiplier <= 0 || stopLossMultiplier >= 1) {
    throw new Error("止损倍数必须是大于0且小于1的数字");
  }
  if (!Number.isInteger(minMarketDurationDays) || minMarketDurationDays < 1 || minMarketDurationDays > 365) {
    throw new Error("最短市场总时长必须是1至365天之间的整数");
  }
  if (!Number.isInteger(maxMarketDurationDays) || maxMarketDurationDays < 1 || maxMarketDurationDays > 365) {
    throw new Error("最长市场总时长必须是1至365天之间的整数");
  }
  if (minMarketDurationDays > maxMarketDurationDays) {
    throw new Error("最短市场总时长不能超过最长市场总时长");
  }
  if (ui.displayMode === "TEST" && (!Number.isFinite(initialCapital) || initialCapital <= 0)) throw new Error("总模拟资金必须大于0");
  if (!Number.isFinite(orderAmount) || orderAmount <= 0) throw new Error("每 Event 每轮金额必须大于0");
  if (ui.displayMode === "TEST" && orderAmount > initialCapital) throw new Error("每 Event 每轮金额不能超过总模拟资金");
  if (!Number.isInteger(minBidAskRatioPercent) || minBidAskRatioPercent < 1 || minBidAskRatioPercent > 100) {
    throw new Error("最低买卖盘比例必须是1至100之间的整数");
  }
  if (!Number.isInteger(maxMarketProgressPercent) || maxMarketProgressPercent < 1 || maxMarketProgressPercent > 100) {
    throw new Error("生命周期进度必须是1至100之间的整数");
  }

  return {
    marketTypes,
    allCategories,
    selectedCategoryIds,
    minBuyPriceCents,
    maxBuyPriceCents,
    targetSellPriceIncreaseCents,
    targetSellPriceMultiplier,
    stopLossEnabled,
    stopLossMultiplier,
    minMarketDurationDays,
    maxMarketDurationDays,
    candidateSortDirection: ui.preferences.candidateSortDirection,
    minBidAskRatioPercent,
    maxMarketProgressPercent,
    ...(ui.displayMode === "TEST" ? { initialCapital } : {}),
    orderAmount,
  };
}

function savedPreferencePayload(overrides = {}) {
  return {
    marketTypes: ui.preferences.marketTypes,
    allCategories: ui.preferences.allCategories,
    selectedCategoryIds: ui.preferences.selectedCategoryIds ?? ui.preferences.selectedCategories,
    minBuyPriceCents: ui.preferences.minBuyPriceCents,
    maxBuyPriceCents: ui.preferences.maxBuyPriceCents,
    targetSellPriceIncreaseCents: ui.preferences.targetSellPriceIncreaseCents,
    targetSellPriceMultiplier: ui.preferences.targetSellPriceMultiplier,
    stopLossEnabled: ui.preferences.stopLossEnabled,
    stopLossMultiplier: ui.preferences.stopLossMultiplier,
    minMarketDurationDays: ui.preferences.minMarketDurationDays,
    maxMarketDurationDays: ui.preferences.maxMarketDurationDays,
    candidateSortDirection: ui.preferences.candidateSortDirection,
    minBidAskRatioPercent: ui.preferences.minBidAskRatioPercent,
    maxMarketProgressPercent: ui.preferences.maxMarketProgressPercent,
    orderAmount: Number(ui.preferences.orderAmount),
    ...overrides,
  };
}

$("#config-toggle").addEventListener("click", () => {
  const open = $("#config-panel").hidden;
  if (open && ui.dashboard) {
    ui.configDirty = false;
    renderPreferences(ui.preferences, ui.dashboard.strategy, true);
  }
  setConfigOpen(open);
});

$("#config-close").addEventListener("click", () => {
  ui.configDirty = false;
  if (ui.dashboard) renderPreferences(ui.preferences, ui.dashboard.strategy, true);
  setConfigOpen(false);
});

$("#config-form").addEventListener("input", () => {
  ui.configDirty = true;
  renderTargetSellFormula();
});

$("#stop-loss-enabled").addEventListener("change", renderStopLossControl);

$("#target-sell-formula").addEventListener("keydown", (event) => {
  const formula = event.currentTarget;
  if (event.key === "ArrowLeft") formula.scrollLeft -= 40;
  else if (event.key === "ArrowRight") formula.scrollLeft += 40;
  else if (event.key === "Home") formula.scrollLeft = 0;
  else if (event.key === "End") formula.scrollLeft = formula.scrollWidth;
  else return;
  event.preventDefault();
});

$("#all-categories").addEventListener("change", () => {
  document.querySelectorAll("[data-category-id]").forEach((input) => {
    input.checked = $("#all-categories").checked;
  });
});

$("#category-options").addEventListener("change", () => {
  const options = Array.from(document.querySelectorAll("[data-category-id]"));
  $("#all-categories").checked =
    options.length > 0 && options.every((input) => input.checked);
});

$("#min-market-duration").addEventListener("input", (event) => {
  $("#min-duration-value").textContent = event.target.value || "—";
});

$("#max-market-duration").addEventListener("input", (event) => {
  $("#max-duration-value").textContent = event.target.value || "—";
});

$("#bid-ask-ratio").addEventListener("input", (event) => {
  $("#bid-ask-ratio-value").textContent = event.target.value;
  event.target.setAttribute("aria-valuetext", `至少${event.target.value}%`);
});

$("#market-progress-filter").addEventListener("input", (event) => {
  $("#market-progress-value").textContent = event.target.value;
  event.target.setAttribute("aria-valuetext", `不超过${event.target.value}%`);
});

$("#config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.submitter || $("#save-config");
  setButtonPending(submit, true, "保存中");
  try {
    const response = await api("/api/test/preferences", {
      method: "PUT",
      body: JSON.stringify(collectConfigPayload()),
    });
    recordMutation();
    ui.preferences = response.preferences;
    ui.strategyStatus = response.strategy.status;
    ui.visibleCandidateCount = 20;
    ui.configDirty = false;
    if (ui.dashboard) {
      ui.dashboard.preferences = response.preferences;
      ui.dashboard.strategy = response.strategy;
      ui.dashboard.capitalEditable = response.capitalEditable;
      renderPreferences(response.preferences, response.strategy, true);
    }
    setConfigOpen(false);
    showMessage(
      response.cancelledBuyCount > 0
        ? `配置已保存，已撤销 ${response.cancelledBuyCount} 张不再适用的旧买单`
        : "配置已保存，市场正在按新条件重新扫描",
    );
    await loadDashboard();
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    setButtonPending(submit, false);
    renderRunControls();
  }
});

$("#run-toggle").addEventListener("click", async () => {
  if (ui.displayMode === "LIVE" && !ui.dashboard?.liveExecutionEnabled) {
    showMessage("LIVE尚未由服务器启用", true);
    return;
  }
  const wasRunning = ui.strategyStatus === "RUNNING";
  const button = $("#run-toggle");
  ui.controlPending = true;
  setButtonPending(button, true, wasRunning ? "暂停中" : "启动中");
  try {
    const response = await api(wasRunning ? "/api/test/pause" : "/api/test/start", { method: "POST" });
    recordMutation();
    ui.strategyStatus = response.strategy.status;
    showMessage(wasRunning ? `${ui.displayMode}已暂停新买入；已有仓位仍继续退出和赎回` : `${ui.displayMode}已开始`);
    await loadDashboard();
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    ui.controlPending = false;
    setButtonPending(button, false);
    renderRunControls();
  }
});

$("#reset-test").addEventListener("click", async () => {
  if (ui.strategyStatus !== "PAUSED") {
    showMessage("请先暂停TEST，再执行重置", true);
    return;
  }
  if (!window.confirm("重置会彻底清空TEST资金、订单、成交、持仓、盈亏、结算和配置。此操作无法恢复，继续吗？")) return;
  if (!window.confirm("最后确认：确定将TEST恢复为100U总资金、每 Event 每轮1U和默认筛选条件吗？")) return;
  const button = $("#reset-test");
  ui.controlPending = true;
  setButtonPending(button, true, "重置中");
  try {
    await api("/api/test/reset", {
      method: "POST",
      body: JSON.stringify({
        confirmation: "RESET TEST",
        finalConfirmation: "RESET TEST AGAIN",
      }),
    });
    recordMutation();
    ui.visibleCandidateCount = 20;
    ui.configDirty = false;
    ui.tradeRecords = [];
    ui.tradeRecordTotalCount = 0;
    ui.tradeRecordsLoaded = false;
    ui.tradeRecordsLoadedAt = 0;
    renderTradeRecords();
    showMessage("TEST已彻底重置，并保持暂停状态");
    await loadDashboard();
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    ui.controlPending = false;
    setButtonPending(button, false);
    renderRunControls();
  }
});

$("#mode-toggle").addEventListener("click", () => {
  if (ui.controlPending || ui.loading) return;
  if (ui.configDirty) { showMessage("请先保存设置或关闭设置面板，再切换模式", true); return; }
  recordMutation();
  ui.tradeRecordsLoaded = false; ui.tradeRecordsLoadedAt = 0; ui.tradeRecords = [];
  ui.visibleCandidateCount = 20;
  ui.displayMode = ui.displayMode === "TEST" ? "LIVE" : "TEST";
  renderModeControl();
  renderRunControls();
  showMessage(
    ui.displayMode === "LIVE"
      ? "已切换到LIVE视图；不会自动启动交易"
      : "已切换到TEST",
  );
  void loadDashboard();
});

$("#sort-toggle").addEventListener("click", async () => {
  const button = $("#sort-toggle");
  if (!ui.preferences || button.disabled) return;
  const direction = button.dataset.nextSort;
  button.disabled = true;
  updateSortToggle(direction);
  try {
    const response = await api("/api/test/preferences", {
      method: "PUT",
      body: JSON.stringify(savedPreferencePayload({ candidateSortDirection: direction })),
    });
    recordMutation();
    ui.preferences = response.preferences;
    ui.visibleCandidateCount = 20;
    showMessage(direction === "ASC" ? "已按市场进度正序排列" : "已按市场进度倒序排列");
    await loadDashboard();
  } catch (error) {
    updateSortToggle(ui.preferences.candidateSortDirection);
    showMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#toggle-positions").addEventListener("click", () => {
  ui.positionsExpanded = !ui.positionsExpanded;
  renderPositions(ui.dashboard?.positions ?? []);
});

$("#trade-records-toggle").addEventListener("click", () => {
  ui.tradeRecordsExpanded = !ui.tradeRecordsExpanded;
  renderTradeRecords();
  if (
    ui.tradeRecordsExpanded &&
    (!ui.tradeRecordsLoaded ||
      Date.now() - ui.tradeRecordsLoadedAt >= TRADE_RECORD_REFRESH_MS)
  ) {
    void loadTradeRecords();
  }
});

$("#load-more").addEventListener("click", async () => {
  ui.visibleCandidateCount = Math.min(ui.displayCandidateCount, ui.visibleCandidateCount + 20);
  await loadDashboard();
});

renderModeControl();
renderRunControls();
renderTradeRecords();
void loadDashboard();
window.setInterval(() => {
  void loadDashboard({ silent: true });
}, 500);
