// Pure display rules. These never decide whether the strategy may trade.
export function displayStatus(dashboard, { lastSuccess = 0, error = null, now = Date.now() } = {}) {
  if (!dashboard || !lastSuccess) return { run: "unknown", feed: "unknown", scan: "unknown", warning: error || "等待数据" };
  if (error || now - lastSuccess > 10000) return { run: "error", feed: "error", scan: "unknown", warning: "页面数据更新中断，显示为上次快照；当前运行状态未确认。" };
  if (dashboard.executionMode === "LIVE" && !dashboard.liveExecutionEnabled) return { run: "off", feed: "off", scan: "off", warning: "" };
  const scan = dashboard.marketScan || {};
  const groups = scan.diagnostics?.streams?.groups || [];
  const fatal = scan.serviceError;
  const feedError = scan.streamError || groups.some(g => g.error || !g.taskRunning || g.state === "STOPPED");
  const ready = groups.length > 0 && groups.every(g => g.state === "READY" && g.readyBookCount === g.tokenCount);
  const missingPosition = (dashboard.positions || []).some(p => p.currentSellPriceStatus === "NOT_READY");
  return {
    run: fatal ? "error" : dashboard.strategy.status === "RUNNING" ? "ready" : "off",
    feed: feedError ? "error" : ready && !missingPosition ? "ready" : "waiting",
    scan: scan.lastError || scan.categoryError ? "error" : scan.scanning ? "waiting" : scan.lastScanAt ? "ready" : "unknown",
    warning: fatal ? `自动暂停：${scan.lastServiceError || fatal}` : missingPosition ? "部分持仓行情未就绪，相关估值未知。" : feedError ? "部分行情正在恢复，展开状态查看详情。" : "",
  };
}

export function modeSwitchDecision({ displayMode, controlPending, configDirty }) {
  if (controlPending) return { nextMode: null, error: "当前操作正在处理，请稍后再切换" };
  if (configDirty) return { nextMode: null, error: "有未保存设置，请先保存；如需放弃草稿，可刷新页面后切换模式" };
  return { nextMode: displayMode === "TEST" ? "LIVE" : "TEST", error: null };
}

export function liveRunSummary(status, { lastSuccess = 0, error = null, now = Date.now() } = {}) {
  const stale = !lastSuccess || Boolean(error) || now - lastSuccess > 10000;
  if (stale) return status === "RUNNING"
    ? { text: "LIVE 上次为运行中 · 待核对", tone: "negative" }
    : { text: "LIVE 状态未确认", tone: "neutral" };
  return {
    RUNNING: { text: "LIVE 运行中", tone: "negative" },
    PAUSED: { text: "LIVE 已暂停", tone: "neutral" },
    LOCKED: { text: "LIVE 已锁定", tone: "neutral" },
    ERROR: { text: "LIVE 状态异常", tone: "negative" },
  }[status] || { text: "LIVE 状态未确认", tone: "neutral" };
}

export function redemptionApprovalAction(wallet, {
  dashboardReady = false, secure = false, pending = false, loading = false,
} = {}) {
  const status = wallet?.redemptionApproval?.status;
  const visible = Boolean(wallet?.configured && wallet?.unlocked && wallet?.enabled === false && wallet?.status !== "RUNNING" &&
    ["MISSING", "PENDING", "FAILED"].includes(status));
  return {
    visible,
    disabled: !visible || !dashboardReady || !secure || pending || loading,
    label: status === "PENDING" ? "继续核对赎回授权" : status === "FAILED" ? "重试赎回授权" : "授权赎回",
  };
}

export function walletOriginSecure({ protocol, hostname }) {
  return protocol === "https:" || (
    protocol === "http:" && ["localhost", "127.0.0.1", "[::1]", "testserver"].includes(hostname)
  );
}

export function liveWalletAccountVerified({ dashboard, wallet, walletAt, error, now = Date.now() }) {
  return Boolean(
    dashboard?.liveExecutionEnabled && wallet?.configured && wallet?.unlocked && wallet?.enabled && wallet?.readiness?.ready === true &&
    !error && now - walletAt < 20_000 && !["BLOCKED", "LOCKED", "ERROR"].includes(wallet?.status)
  );
}

export function curveSegments(points) {
  const segments = []; let segment = [], previous = null;
  for (const p of points) {
    const valid = p.pnl !== null && p.pnl !== undefined && Number.isFinite(Number(p.pnl)) && Number.isFinite(p.at);
    const buckets = previous && Number.isInteger(previous.bucket) && Number.isInteger(p.bucket);
    const interrupted = previous && (
      previous.session !== p.session || p.at <= previous.at || p.breakBefore ||
      (buckets ? p.bucket !== previous.bucket + 1 : p.at - previous.at > 90000)
    );
    if (!valid || interrupted) {
      if (segment.length) segments.push(segment);
      segment = [];
    }
    if (valid) segment.push(p);
    previous = valid ? p : null;
  }
  if (segment.length) segments.push(segment);
  return segments;
}
