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

export function curveSegments(points) {
  const segments = []; let segment = [], previous = null;
  for (const p of points) {
    const valid = p.pnl !== null && p.pnl !== undefined && Number.isFinite(Number(p.pnl)) && Number.isFinite(p.at);
    if (!valid || previous && (previous.session !== p.session || p.at <= previous.at || p.at - previous.at > 90000)) {
      if (segment.length) segments.push(segment);
      segment = [];
    }
    if (valid) segment.push(p);
    previous = valid ? p : null;
  }
  if (segment.length) segments.push(segment);
  return segments;
}
