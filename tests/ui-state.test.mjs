import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness,profile,seedTestDashboard} from './ui-app-harness.mjs';
import {displayStatus,curveSegments,modeSwitchDecision,walletOriginSecure,liveWalletAccountVerified,liveRunSummary,redemptionApprovalAction,panelVisible,setMarkupIfChanged} from '../src/pm_nautilus/web/ui-state.js';
const base=()=>({executionMode:'TEST',strategy:{status:'RUNNING'},positions:[{currentSellPriceStatus:'NO_BID'}],marketScan:{lastScanAt:'2026-09-22',diagnostics:{streams:{groups:[{taskRunning:true,state:'READY',readyBookCount:2,tokenCount:2}]}}}});
const opts={lastSuccess:1000,now:2000};
test('storage protection is visible and blocks START while preserving PAUSE',()=>{
 const page=pageHarness();seedTestDashboard(page);
 page.ui.dashboard.storage={status:'CRITICAL',canOpen:false};
 page.context.renderRunControls();page.context.renderStatus();
 assert.equal(page.element('#run-toggle').disabled,true);
 assert.equal(page.element('#storage-warning').hidden,false);
 assert.match(page.element('#storage-warning').textContent,/存储保护/);
 page.ui.strategyStatus='RUNNING';page.ui.dashboard.strategy.status='RUNNING';
 page.context.renderRunControls();
 assert.equal(page.element('#run-toggle').disabled,false);
 page.ui.dashboard.storage={status:'OK',canOpen:true};page.context.renderStatus();
 assert.equal(page.element('#storage-warning').hidden,true);
});
test('query freshness concern is separate from healthy prices and clears on recovery',()=>{
 const dashboard=base();
 const normal={enabled:true,status:'ok',lastSuccessAt:'2026-10-21T08:00:00Z',consecutiveFailures:0,error:null};
 dashboard.serviceFreshness={discovery:{...normal,status:'retrying',error:'HTTPStatusError',consecutiveFailures:1},settlement:{...normal}};
 let state=displayStatus(dashboard,opts);
 assert.equal(state.scan,'waiting');assert.equal(state.serviceWarning,'');assert.equal(state.run,'ready');assert.equal(state.feed,'ready');
 dashboard.serviceFreshness.settlement={...normal,status:'attention',reason:'query_failed',error:'ReadTimeout',consecutiveFailures:3,failureDurationSeconds:90,affectedMarketCount:1};
 state=displayStatus(dashboard,opts);
 assert.match(state.serviceWarning,/结算查询需关注/);assert.match(state.serviceWarning,/连续失败 3 次/);assert.match(state.serviceWarning,/影响 1 个市场/);
 assert.equal(state.warning,'');assert.equal(state.run,'ready');assert.equal(state.feed,'ready');
 dashboard.serviceFreshness.discovery={...normal,status:'attention',reason:'stale'};
 assert.equal(displayStatus(dashboard,opts).scan,'warning');
 assert.match(displayStatus(dashboard,opts).serviceWarning,/超过 15 分钟/);
 dashboard.serviceFreshness={discovery:{...normal},settlement:{...normal}};
 state=displayStatus(dashboard,opts);assert.equal(state.serviceWarning,'');assert.equal(state.scan,'ready');
});
test('actual page shows current query concern and details while keeping usable assets visible',()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context,element}=page;
 ui.dashboard={...base(),serviceFreshness:{settlement:{enabled:true,status:'attention',reason:'query_failed',lastSuccessAt:'2026-10-21T08:00:00Z',error:'ReadTimeout',consecutiveFailures:4,failureDurationSeconds:120,affectedMarketCount:2}}};
 ui.activeTab='positions';context.renderStatus();
 assert.equal(element('#service-warning').hidden,false);assert.match(element('#service-warning').textContent,/结算查询需关注/);
 assert.match(element('#module-status-detail').textContent,/ReadTimeout/);assert.match(element('#module-status-detail').textContent,/影响 2 个市场/);
 assert.equal(element('#run-dot').dataset.state,'ready');assert.equal(element('#positions-dot').dataset.state,'ready');
 assert.equal(element('#connection-warning').hidden,true);
 ui.dashboard.serviceFreshness.settlement={enabled:true,status:'ok',lastSuccessAt:'2026-10-21T08:03:00Z',consecutiveFailures:0};context.renderStatus();
 assert.equal(element('#service-warning').hidden,true);assert.equal(element('#module-status-detail').textContent.includes('ReadTimeout'),false);
});
test('query freshness never replays another mode or a stale dashboard as a current concern',()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context,element}=page;
 ui.dashboard={...base(),serviceFreshness:{settlement:{enabled:true,status:'attention',reason:'query_failed',error:'ReadTimeout',consecutiveFailures:4}}};
 ui.dashboardAt=Date.now()-11_000;context.renderStatus();
 assert.equal(element('#service-warning').hidden,true);assert.equal(element('#module-status-detail').textContent.includes('ReadTimeout'),false);
 assert.match(element('#connection-warning').textContent,/上次快照/);
 ui.dashboardAt=Date.now();ui.displayMode='LIVE';context.renderStatus();
 assert.equal(element('#service-warning').hidden,true);assert.equal(element('#module-status-detail').textContent.includes('ReadTimeout'),false);
});
test('only the visible panel needs list rendering',()=>{
 for (const active of ['positions','market','records']) {
  for (const panel of ['positions','market','records']) assert.equal(panelVisible(active,panel),active===panel);
 }
});
test('unchanged list markup preserves nodes and changing status invalidates the cache',()=>{
 let writes=0,content='';
 const element={dataset:{},set innerHTML(value){writes++;content=value;},get innerHTML(){return content;}};
 assert.equal(setMarkupIfChanged(element,'records'),true);
 assert.equal(setMarkupIfChanged(element,'records'),false);
 assert.equal(writes,1);
 assert.equal(setMarkupIfChanged(element,'locked'),true);
 assert.equal(setMarkupIfChanged(element,'records'),true);
 assert.equal(writes,3);
 assert.equal(content,'records');
});
test('quiet/no-bid is healthy; stale UI cannot imply running',()=>{
 assert.equal(displayStatus(base(),opts).feed,'ready');
 assert.equal(displayStatus(base(),{...opts,now:12000}).run,'error');
 assert.equal(displayStatus(base(),{...opts,error:'offline'}).feed,'error');
});
test('fatal, missing books and scanning have separate status',()=>{
 const d=base();d.marketScan.scanning=true;assert.equal(displayStatus(d,opts).scan,'waiting');
 d.positions[0].currentSellPriceStatus='NOT_READY';assert.equal(displayStatus(d,opts).feed,'waiting');
 d.marketScan.serviceError='fatal';assert.equal(displayStatus(d,opts).run,'error');
 d.executionMode='LIVE';d.liveExecutionEnabled=false;assert.equal(displayStatus(d,opts).run,'off');
});
test('curve never joins unknown, restart or unsampled interval',()=>{
 const p=(at,pnl,session='a')=>({at,pnl,session});
 const series=curveSegments([p(0,'0'),p(60000,'1'),p(120000,null),p(180000,'-1'),p(240000,'2','b'),p(400000,'3','b')]);
 assert.deepEqual(series.map(s=>s.length),[2,1,1,1]);
 assert.equal(curveSegments([p(0,null)]).length,0);
});
test('mode switch remains available during background polling and never starts trading',()=>{
 const state={displayMode:'TEST',loading:true,performanceLoading:true,tradeRecordsLoading:true,controlPending:false,configDirty:false};
 assert.deepEqual(modeSwitchDecision(state),{nextMode:'LIVE',error:null});
 assert.deepEqual(modeSwitchDecision({...state,displayMode:'LIVE'}),{nextMode:'TEST',error:null});
 assert.equal(modeSwitchDecision({...state,controlPending:true}).nextMode,null);
 assert.equal(modeSwitchDecision({...state,configDirty:true}).nextMode,null);
});
test('LIVE run state remains visible across mode views and does not imply pause when stale',()=>{
 assert.deepEqual(liveRunSummary('RUNNING',{lastSuccess:1000,now:2000}),{text:'LIVE 运行中',tone:'negative'});
 assert.deepEqual(liveRunSummary('RUNNING',{lastSuccess:1000,now:12000}),{text:'LIVE 上次为运行中 · 待核对',tone:'negative'});
 assert.deepEqual(liveRunSummary('PAUSED',{lastSuccess:1000,now:12000}),{text:'LIVE 状态未确认',tone:'neutral'});
 assert.equal(liveRunSummary('LOCKED',{lastSuccess:1000,now:2000}).text,'LIVE 已锁定');
});
test('redemption approval is offered only for an unlocked, stopped wallet needing action',()=>{
 const wallet={configured:true,unlocked:true,enabled:false,status:'BLOCKED',redemptionApproval:{status:'MISSING'}};
 const ready={dashboardReady:true,secure:true};
 assert.deepEqual(redemptionApprovalAction(wallet,ready),{visible:true,disabled:false,label:'授权赎回'});
 assert.equal(redemptionApprovalAction({...wallet,status:'RUNNING'},ready).visible,false);
 assert.equal(redemptionApprovalAction({...wallet,enabled:true,status:'PAUSED'},ready).visible,false);
 assert.equal(redemptionApprovalAction({...wallet,unlocked:false},ready).visible,false);
 assert.equal(redemptionApprovalAction({...wallet,redemptionApproval:{status:'APPROVED'}},ready).visible,false);
 assert.equal(redemptionApprovalAction({...wallet,redemptionApproval:{status:'PENDING'}},ready).label,'继续核对赎回授权');
 assert.equal(redemptionApprovalAction(wallet,{...ready,secure:false}).disabled,true);
});
test('hour and day points do not connect across an interrupted bucket',()=>{
 const p=(bucket,breakBefore=false,session='a')=>({at:bucket*3600000+3599000,bucket,breakBefore,session,pnl:'1'});
 assert.deepEqual(curveSegments([p(0),p(1),p(2,true),p(3),p(5),p(6,false,'b')]).map(s=>s.length),[2,2,1,1]);
});
test('wallet secrets require HTTPS except loopback development',()=>{
 assert.equal(walletOriginSecure({protocol:'https:',hostname:'trading.example'}),true);
 assert.equal(walletOriginSecure({protocol:'http:',hostname:'trading.example'}),false);
 assert.equal(walletOriginSecure({protocol:'http:',hostname:'127.0.0.1'}),true);
 assert.equal(walletOriginSecure({protocol:'http:',hostname:'localhost'}),true);
});
test('LIVE funds and start require fresh unlocked enabled wallet state',()=>{
 const state={dashboard:{liveExecutionEnabled:true},wallet:{configured:true,unlocked:true,enabled:true,status:'PAUSED',readiness:{ready:true}},walletAt:1000,now:2000,error:null};
 assert.equal(liveWalletAccountVerified(state),true);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,unlocked:false}}),false);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,status:'BLOCKED'}}),false);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,readiness:{ready:false}}}),false);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,enabled:false}}),false);
 assert.equal(liveWalletAccountVerified({...state,dashboard:{liveExecutionEnabled:false}}),false);
 assert.equal(liveWalletAccountVerified({...state,now:22000}),false);
 assert.equal(liveWalletAccountVerified({...state,error:'unavailable'}),false);
});

test('switching modes clears hidden market data while the next dashboard is pending',()=>{
 const page=pageHarness(), {ui,element}=page;
 seedTestDashboard(page);
 ui.events=[{eventTitle:'TEST_ONLY_MARKET',representative:{marketQuestion:'TEST_ONLY_MARKET',categoryLabels:[]},progressPercent:20,status:'NO_WINNER',outcomes:[]}];
 ui.candidateCount=1;ui.displayCandidateCount=1;ui.staleCandidateCount=1;
 element('#mode-toggle').handlers.click();
 assert.equal(ui.displayMode,'LIVE');
 assert.equal(ui.dashboard,null);
 assert.equal(ui.events.length,0);
 assert.equal(ui.candidateCount,0);
 assert.equal(ui.displayCandidateCount,0);
 assert.equal(ui.staleCandidateCount,0);
 element('#tab-market').handlers.click();
 assert.equal(element('#candidates').innerHTML.includes('TEST_ONLY_MARKET'),false);
 assert.equal(element('#candidate-count').textContent,'读取中');
 assert.equal(element('#load-more').disabled,true);
 // An unrelated cached dashboard cannot authorize rendering another mode's data.
 ui.dashboard={executionMode:'TEST'};
 ui.events=[{eventTitle:'TEST_ONLY_MARKET'}];
 page.context.renderCandidatesForTest();
 assert.equal(element('#candidates').innerHTML.includes('TEST_ONLY_MARKET'),false);
});

for (const [control,eventType,url] of [
 ['sort-toggle','click','/api/test/preferences'],
 ['config-form','submit','/api/test/preferences'],
 ['run-toggle','click','/api/test/start'],
 ['reset-test','click','/api/test/reset'],
]) test(`${control} serializes controls and ignores a reply from an obsolete mode`,async()=>{
 const page=pageHarness(), {ui,element,context}=page;
 seedTestDashboard(page);
 let resolveMutation,requests=0;
 context.fetch=(path)=>path.split('?')[0]===url?new Promise(resolve=>{requests++;resolveMutation=resolve;}):new Promise(()=>{});
 const pending=element(`#${control}`).handlers[eventType]({preventDefault(){}});
 assert.equal(requests,1);
 assert.equal(ui.controlPending,true);
 element('#mode-toggle').handlers.click();
 assert.equal(ui.displayMode,'TEST');
 await element('#sort-toggle').handlers.click();
 await element('#run-toggle').handlers.click();
 assert.equal(requests,1);
 // Even a view replaced outside the click handler must reject the old reply.
 const livePreferences={...profile,orderAmount:'7'};
 ui.displayMode='LIVE';ui.mutationVersion++;
 ui.dashboard={executionMode:'LIVE',preferences:livePreferences,strategy:{status:'LOCKED'}};
 ui.preferences=livePreferences;ui.strategyStatus='LOCKED';
 resolveMutation({ok:true,headers:{get(){return null;}},async text(){return JSON.stringify({preferences:{...profile,candidateSortDirection:'DESC'},strategy:{status:'RUNNING'}});}});
 await pending;
 assert.equal(ui.preferences,livePreferences);
 assert.equal(ui.dashboard.preferences,livePreferences);
 assert.equal(ui.strategyStatus,'LOCKED');
 assert.equal(ui.controlPending,false);
});

test('a failed stale sort reply does not read cleared preferences or show another mode error',async()=>{
 const page=pageHarness(), {ui,element,context}=page;
 seedTestDashboard(page);
 let resolveSort;
 context.fetch=path=>path.split('?')[0]==='/api/test/preferences'?new Promise(resolve=>{resolveSort=resolve;}):new Promise(()=>{});
 const pending=element('#sort-toggle').handlers.click();
 ui.displayMode='LIVE';ui.mutationVersion++;ui.preferences=null;ui.dashboard=null;
 const message=element('#message').textContent;
 resolveSort({ok:false,headers:{get(){return null;}},async text(){return JSON.stringify({error:'old TEST failure'});}});
 await pending;
 assert.equal(element('#message').textContent,message);
 assert.equal(ui.preferences,null);
 assert.equal(element('#sort-toggle').disabled,true);
});


test('summary readiness includes hidden positions',()=>{
 const dashboard=base();dashboard.positions=[];dashboard.positionBooksReady=false;
 assert.equal(displayStatus(dashboard,{lastSuccess:1000,now:2000}).feed,'waiting');
});

test('known unavailable rights stay visible separately from healthy subscription feeds',()=>{
 const dashboard={...base(),positions:[],positionCount:92,positionBooksReady:false,unavailablePositionCount:1,unvaluedPositionCount:1,notReadyPositionCount:0};
 const state=displayStatus(dashboard,opts);
 assert.equal(state.feed,'ready');assert.equal(state.run,'ready');assert.equal(state.positions,'warning');
 assert.equal(state.warning,'');assert.match(state.positionNotice,/1 笔持仓/);assert.match(state.positionNotice,/处置安排待核查/);
 assert.equal(state.warning.includes('行情未就绪'),false);
 dashboard.notReadyPositionCount=1;
 assert.equal(displayStatus(dashboard,opts).feed,'waiting');
 assert.equal(displayStatus(dashboard,opts).warning,'');
 dashboard.marketScan.streamError='connection failed';
 assert.equal(displayStatus(dashboard,opts).feed,'error');
 assert.match(displayStatus(dashboard,opts).positionNotice,/市场已停用或归档/);assert.match(displayStatus(dashboard,opts).warning,/行情正在恢复/);
});

test('all owned markets unavailable with no groups never appear ready or endlessly reconnecting',()=>{
 const dashboard={...base(),positions:[],positionCount:1,positionBooksReady:false,unavailablePositionCount:1,unvaluedPositionCount:1,notReadyPositionCount:0,marketScan:{lastScanAt:'2026-10-20',diagnostics:{streams:{groups:[]}}}};
 const state=displayStatus(dashboard,opts);
 assert.equal(state.feed,'unavailable');assert.equal(state.positions,'warning');
 assert.equal(state.warning,'');assert.match(state.positionNotice,/报价不可用/);
 const unknown={...dashboard,unavailablePositionCount:0};
 assert.equal(displayStatus(unknown,opts).feed,'waiting');
});

function costBasisDashboard(positions, overrides={}) {
 const count=positions.length,unpriced=positions.filter(p=>['UNAVAILABLE','NOT_READY'].includes(p.currentSellPriceStatus)).length;
 return {...base(),generation:'g',details:'positions',preferences:{...profile},strategy:{status:'RUNNING',initialCapital:'100',availableCash:'81'},positions,positionCount:count,positionBooksReady:!unpriced,valuedPositionCount:count-unpriced,unvaluedPositionCount:unpriced,unavailablePositionCount:positions.filter(p=>p.currentSellPriceStatus==='UNAVAILABLE').length,notReadyPositionCount:positions.filter(p=>p.currentSellPriceStatus==='NOT_READY').length,portfolio:{totalFunds:null,unrealizedPnl:null,positionValue:null,realizedPnl:'2',availableCash:'81',knownPositionValue:'15',unpricedPositionCost:'1',estimatedPositionValue:'16',estimatedTotalFunds:'97',knownUnrealizedPnl:'-5',knownTotalPnl:'-3',valuedPositionCount:count-unpriced,unvaluedPositionCount:unpriced,unavailablePositionCount:unpriced,failedRedemptionCount:0},marketScan:{...base().marketScan,events:[],diagnostics:{...base().marketScan.diagnostics,availableCategories:[]}},...overrides};
}
function normalPosition(n,status='READY') {
 return {tokenId:`normal-${n}`,marketQuestion:`NORMAL_${n}`,direction:'Yes',quantity:'50',averageBuyPrice:'0.02',remainingCost:'1',unrealizedPnl:status==='NO_BID'?'-1':'-0.25',estimatedUnrealizedPnl:status==='NO_BID'?'-1':'-0.25',currentSellPrice:'0.015',currentSellPriceStatus:status,executableSellValue:status==='NO_BID'?'0':'0.75',executableSellQuantity:status==='NO_BID'?'0':'50',targetSellPrice:'0.04',targetSellPrices:['0.04'],stopLossThreshold:null,cycleStatus:'ACCUMULATING'};
}
function unavailablePosition(status='UNAVAILABLE') {
 return {...normalPosition('archived'),tokenId:'archived',marketQuestion:'ARCHIVED_MARKET',currentSellPriceStatus:status,marketAvailability:{status:'UNAVAILABLE',reason:'archived',since:'2026-10-20T05:00:00Z'},currentSellPrice:null,executableSellValue:null,executableSellQuantity:null,unrealizedPnl:null,estimatedUnrealizedPnl:'0'};
}

test('actual page shows normal holdings first and cost basis for the one unavailable holding',()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context,element}=page;
 const archived=unavailablePosition();
 const positions=[archived,...Array.from({length:20},(_,i)=>normalPosition(i+1))];
 const dashboard=costBasisDashboard(positions);
 context.applyDashboardForTest(dashboard);
 assert.equal(element('#total-funds').textContent,'97.00U');assert.equal(element('#net-pnl').textContent,'-3.00U');
 assert.equal(element('#available-cash').textContent,'81.00U');assert.equal(element('#realized-pnl').textContent,'+2.00U');
 assert.equal(element('#unrealized-pnl').textContent,'-5.00U');
 assert.equal(element('#known-position-value').textContent,'15.00U');
 assert.equal(element('#position-value').textContent,'16.00U');
 assert.equal(element('#unpriced-position-cost').textContent,'1.00U');
 assert.equal(element('#valuation-note').hidden,false);
 assert.match(element('#valuation-note').textContent,/1 笔暂无报价/);assert.match(element('#valuation-note').textContent,/浮盈亏暂记 0/);assert.equal(element('#net-pnl-label').textContent,'累计盈亏估算');
 assert.match(element('#valuation-note').textContent,/成本暂计 1.00U/);
 assert.match(element('#valuation-detail').textContent,/剩余成本 1.00U 计入/);
 assert.match(element('#valuation-detail').textContent,/曲线显示历史记录/);assert.match(element('#valuation-detail').textContent,/恢复报价后重新计算/);
 assert.equal(element('#tab-position-count').textContent,'21');assert.match(element('#position-count').textContent,/1笔暂无报价/);
 assert.equal(element('#connection-warning').hidden,true);
 assert.match(element('#positions').innerHTML,/NORMAL_1/);assert.match(element('#positions').innerHTML,/NORMAL_20/);
 assert.equal(element('#positions').innerHTML.includes('ARCHIVED_MARKET'),false);
 assert.equal((element('#positions').innerHTML.match(/<details class="position-row/g)||[]).length,20);
 assert.match(element('#positions').innerHTML,/剩余成本 <strong>1.00U/);
 assert.match(element('#positions').innerHTML,/浮盈亏 <strong data-tone="negative">-0.25U/);
 // The source response is untouched; sorting is stable within each quote group.
 assert.equal(dashboard.positions[0].tokenId,'archived');
 ui.positionsExpanded=true;context.renderPositions(dashboard.positions);
 const markup=element('#positions').innerHTML;
 assert.ok(markup.indexOf('NORMAL_20')<markup.indexOf('ARCHIVED_MARKET'));
 assert.match(markup,/已归档 · 报价不可用/);assert.match(markup,/处置安排待核查/);
 assert.match(markup,/交易不可用 · 处置待核查/);assert.match(markup,/浮盈亏 <strong data-tone="neutral">0.00U（暂计）/);
 assert.equal(element('#positions-dot').dataset.state,'warning');
 assert.match(element('#module-status-label').textContent,/行情正常 · 1 笔暂无报价/);
 assert.match(element('#module-status-detail').textContent,/2\/2 盘口完整/);
 // Full-account money refreshes from the new summary while rows stay available.
 context.applyDashboardForTest({...dashboard,details:'summary',positions:[],portfolio:{...dashboard.portfolio,estimatedTotalFunds:'98',knownTotalPnl:'-2'}});
 assert.match(element('#positions').innerHTML,/NORMAL_20/);
 assert.equal(element('#total-funds').textContent,'98.00U');assert.equal(element('#net-pnl').textContent,'-2.00U');
 assert.equal(element('#position-refresh-note').hidden,false);
 assert.match(element('#position-refresh-note').textContent,/上次持仓明细/);
 context.applyDashboardForTest({...dashboard,generation:'new',details:'summary',positions:[]});
 assert.equal(element('#positions').innerHTML.includes('NORMAL_20'),false);
});

test('unknown quotes keep realized profit and cost assets without inventing floating profit or history',()=>{
 for (const realized of ['-2','0','2']) {
  const page=pageHarness();seedTestDashboard(page);const {ui,context,element}=page;
  const positions=[unavailablePosition(),unavailablePosition('NOT_READY')];
  const dashboard=costBasisDashboard(positions);
  dashboard.portfolio={...dashboard.portfolio,knownPositionValue:'0',unpricedPositionCost:'2',estimatedPositionValue:'2',estimatedTotalFunds:'100',realizedPnl:realized,knownUnrealizedPnl:'0',knownTotalPnl:realized};
  const points=[{at:1000,pnl:'1',session:'s'},{at:2000,pnl:null,session:'s'}];
  ui.performance={generation:'g',points};ui.performanceAt=Date.now();
  context.applyDashboardForTest(dashboard);
  assert.equal(element('#total-funds').textContent,'100.00U');
  assert.equal(element('#unrealized-pnl').textContent,'0.00U');
  assert.equal(element('#net-pnl').textContent,`${Number(realized)>0?'+':''}${Number(realized).toFixed(2)}U`);
  assert.equal(element('#net-pnl').dataset.tone,Number(realized)>0?'positive':Number(realized)<0?'negative':'neutral');
  assert.match(element('#valuation-note').textContent,/2 笔暂无报价/);assert.match(element('#positions').innerHTML,/当前买盘可卖 <strong>暂无报价/);
  assert.match(element('#positions').innerHTML,/浮盈亏 <strong data-tone="neutral">0.00U（暂计）/);
  assert.equal(ui.performance.points,points);assert.equal(ui.performance.points[1].pnl,null);
 }
});

test('failed redemption fallback remains cost assets, retains realized PnL and avoids cash duplication',()=>{
 const page=pageHarness();seedTestDashboard(page);const {context,element}=page;
 const dashboard=costBasisDashboard([]);
 dashboard.portfolio={...dashboard.portfolio,availableCash:'100',knownPositionValue:'0',unpricedPositionCost:'0',estimatedPositionValue:'0',estimatedTotalFunds:'102',knownUnrealizedPnl:'0',knownTotalPnl:'1.5',realizedPnl:'1.5',failedRedemption:'99',failedRedemptionCost:'5',failedRedemptionCostFallback:'2',failedRedemptionCount:2,failedRedemptionCashIncludedCount:1};
 context.applyDashboardForTest(dashboard);
 assert.equal(element('#total-funds').textContent,'102.00U');assert.equal(element('#net-pnl').textContent,'+1.50U');
 assert.equal(element('#valuation-note').textContent,'2 笔赎回待核查');
 assert.match(element('#valuation-detail').textContent,/2 笔赎回处置待核查，浮盈亏暂记 0/);
 assert.match(element('#valuation-detail').textContent,/2.00U 成本暂计/);
 assert.match(element('#valuation-detail').textContent,/1 笔已计现金的部分不重复/);
 assert.equal(element('#valuation-detail').textContent.includes('99.00U'),false);
});

test('legacy normal snapshots keep figures and ready no-bid holdings stay before unavailable quotes',()=>{
 const page=pageHarness();seedTestDashboard(page);const {context,element}=page;
 const dashboard=costBasisDashboard([unavailablePosition(),normalPosition(1,'NO_BID'),normalPosition(2)]);
 dashboard.portfolio={totalFunds:'100',realizedPnl:'2',unrealizedPnl:'1',positionValue:'5',availableCash:'95'};
 context.applyDashboardForTest(dashboard);
 assert.equal(element('#total-funds').textContent,'100.00U');assert.equal(element('#net-pnl').textContent,'+3.00U');
 const markup=element('#positions').innerHTML;
 assert.ok(markup.indexOf('NORMAL_1')<markup.indexOf('NORMAL_2'));
 assert.ok(markup.indexOf('NORMAL_2')<markup.indexOf('ARCHIVED_MARKET'));
 assert.match(markup,/暂无买盘/);assert.match(markup,/data-tone="negative">-1.00U/);
 assert.equal(element('#valuation-note').hidden,true);
});

test('locked unverified stale or mismatched LIVE snapshots hide new estimates and cached positions',()=>{
 for (const fault of ['locked','stale','read-error','unavailable','wrong-mode']) {
  const page=pageHarness();seedTestDashboard(page);const {ui,context,element}=page;
  ui.displayMode='LIVE';ui.wallet={configured:true,unlocked:true,enabled:true,status:'PAUSED',readiness:{ready:true}};ui.walletAt=Date.now();
  const dashboard=costBasisDashboard([normalPosition(1)],{executionMode:'LIVE',liveExecutionEnabled:true});
  context.applyDashboardForTest(dashboard);assert.match(element('#positions').innerHTML,/NORMAL_1/);
  if(fault==='locked')ui.wallet={...ui.wallet,unlocked:false,status:'LOCKED'};
  if(fault==='stale')ui.walletAt=Date.now()-21000;
  if(fault==='read-error')ui.walletError='failed';
  const reply={...dashboard,details:'summary',positions:[],portfolio:{...dashboard.portfolio,estimatedTotalFunds:'999'}};
  if(fault==='unavailable')reply.positionsAvailable=false;
  if(fault==='wrong-mode')reply.executionMode='TEST';
  context.applyDashboardForTest(reply);
  assert.equal(element('#total-funds').textContent,'—');assert.equal(element('#net-pnl').textContent,'—');
  assert.equal(element('#positions').innerHTML.includes('NORMAL_1'),false);
  assert.equal(ui.positionSnapshot,null);assert.equal(element('#valuation-note').hidden,true);
 }
});

test('summary and market retain only verified same-mode same-generation details while refreshing counts',()=>{
 for (const mode of ['TEST','LIVE']) {
  const page=pageHarness();seedTestDashboard(page);
  const {ui,context,element}=page;
  ui.displayMode=mode;ui.activeTab='positions';
  ui.wallet={configured:true,unlocked:true,enabled:true,status:'PAUSED',readiness:{ready:true}};
  ui.walletAt=Date.now();
  const dashboard={executionMode:mode,generation:'g',details:'positions',liveExecutionEnabled:true,preferences:{...profile},strategy:{status:'PAUSED',initialCapital:'100',availableCash:'100'},positions:[{tokenId:'owned',marketQuestion:'LOADED_POSITION',quantity:'1',targetSellPrices:[],stopLossThreshold:null}],positionCount:86,positionBooksReady:true,portfolio:{},marketScan:{events:[],diagnostics:{availableCategories:[]}}};
  context.applyDashboardForTest(dashboard);
  assert.equal(element('#tab-position-count').textContent,'86');
  assert.match(element('#positions').innerHTML,/LOADED_POSITION/);
  for (const details of ['summary','market']) {
   ui.activeTab='positions';
   context.applyDashboardForTest(dashboard);
   ui.activeTab=details==='summary'?'records':'market';
   context.applyDashboardForTest({...dashboard,details,positions:[]});
   assert.equal(element('#tab-position-count').textContent,'86');
   assert.equal(element('#position-count').textContent,'86单');
   assert.equal(element('#position-refresh-note').hidden,false);
   assert.match(element('#positions').innerHTML,/LOADED_POSITION/);
   for (const positionCount of [0,-1,1.5,'86',null,undefined]) {
    context.applyDashboardForTest({...dashboard,details,positions:[],positionCount});
    assert.equal(element('#tab-position-count').textContent,positionCount===0?'0':'—');
   }
   context.applyDashboardForTest({...dashboard,details,positions:[],executionMode:mode==='LIVE'?'TEST':'LIVE'});
   assert.equal(element('#tab-position-count').textContent,'—');
   if (mode==='LIVE') {
    for (const wallet of [null,{...ui.wallet,unlocked:false},{...ui.wallet,status:'LOCKED'},{...ui.wallet,readiness:{ready:false}}]) {
     const verified=ui.wallet;ui.wallet=wallet;
     context.applyDashboardForTest({...dashboard,details,positions:[]});
     assert.equal(element('#tab-position-count').textContent,'—');
     ui.wallet=verified;
    }
    ui.walletAt=Date.now()-21_000;
    context.applyDashboardForTest({...dashboard,details,positions:[]});
    assert.equal(element('#tab-position-count').textContent,'—');
    ui.walletAt=Date.now();ui.walletError='unavailable';
    context.applyDashboardForTest({...dashboard,details,positions:[]});
    assert.equal(element('#tab-position-count').textContent,'—');
    ui.walletError=null;
    context.applyDashboardForTest({...dashboard,details,positions:[],liveExecutionEnabled:false,positionCount:0});
    assert.equal(element('#tab-position-count').textContent,'—');
   }
  }
 }
});

test('history pagination is disabled during a pending refresh and resumes with its cursor',async()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context,element}=page;ui.activeTab='records';ui.tradeRecordsExpanded=true;
 const row={id:'first',type:'OPEN',firstSeq:40,quantity:'1'};
 const reply=body=>({ok:true,headers:{get(){return null;}},async text(){return JSON.stringify(body);}});
 context.fetch=async()=>reply({generation:'g',head:'g:40',totalCount:40,nextCursor:'g:21',records:[row]});
 await context.loadTradeRecordsForTest();
 assert.equal(element('#more-records').hidden,false);
 assert.equal(element('#more-records').disabled,false);
 let resolveRefresh,resolveMore;const requests=[];
 context.fetch=path=>{requests.push(path);return new Promise(resolve=>{if(requests.length===1)resolveRefresh=resolve;else resolveMore=resolve;});};
 const refresh=context.loadTradeRecordsForTest({silent:true});
 assert.equal(element('#more-records').disabled,true);
 resolveRefresh(reply({generation:'g',head:'g:40',totalCount:40,nextCursor:'g:21',records:[row]}));
 await refresh;
 assert.equal(element('#more-records').disabled,false);
 element('#more-records').handlers.click();
 assert.equal(requests.length,2);
 assert.match(requests[1],/cursor=g%3A21/);
 assert.equal(element('#more-records').disabled,true);
 resolveMore(reply({generation:'g',head:'g:40',totalCount:40,nextCursor:'g:1',records:[{id:'older',type:'OPEN',firstSeq:20,quantity:'1'}]}));
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(ui.tradeRecordsLoading,false);
 assert.equal(element('#more-records').disabled,false);
 assert.deepEqual(Array.from(ui.tradeRecords,r=>r.id),['first','older']);
});

test('unchanged category refresh preserves focused input nodes',()=>{
 const page=pageHarness();seedTestDashboard(page);
 const el=page.element('#category-options');let writes=0, markup='';
 Object.defineProperty(el,'innerHTML',{get(){return markup;},set(value){writes++;markup=value;}});
 page.context.renderCategoriesForTest(page.ui.preferences);
 page.context.document.activeElement=el;
 page.context.renderCategoriesForTest(page.ui.preferences);
 assert.equal(writes,1);assert.equal(page.context.document.activeElement,el);
});

test('record paging crosses ten thousand and refreshes late fills without losing expanded history',async()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context}=page;ui.activeTab='records';ui.tradeRecordsExpanded=true;
 const replies=[
  {generation:'g',head:'g:10005',totalCount:10005,nextCursor:'g:10004',records:[{id:'new',firstSeq:10005,quantity:'1'},{id:'old',firstSeq:10004,quantity:'1'}]},
  {generation:'g',head:'g:10005',totalCount:10005,nextCursor:'g:3',records:[{id:'beyond-10000',firstSeq:3,quantity:'1'}]},
  {generation:'g',head:'g:10007',totalCount:10006,nextCursor:'g:10005',records:[{id:'newest',firstSeq:10006,quantity:'1'},{id:'new',firstSeq:10005,quantity:'1'}],changes:[{id:'beyond-10000',firstSeq:3,quantity:'2'},{id:'newest',firstSeq:10006,quantity:'1'}]},
 ];
 const requests=[];
 context.fetch=async path=>{requests.push(path);return {ok:true,headers:{get(){return null;}},async text(){const reply=replies.shift();for(const row of [...reply.records,...(reply.changes??[])])row.type="OPEN";return JSON.stringify(reply);}};};
 await context.loadTradeRecordsForTest();
 await context.loadTradeRecordsForTest({more:true});
 assert.match(requests[1],/cursor=g%3A10004/);
 await context.loadTradeRecordsForTest({silent:true});
 assert.match(requests[2],/since=g%3A10005/);
 assert.deepEqual(Array.from(ui.tradeRecords,r=>r.id),['newest','new','old','beyond-10000']);
 assert.equal(ui.tradeRecords.at(-1).quantity,'2');assert.equal(ui.recordCursor,'g:3');
 assert.equal(ui.tradeRecordTotalCount,10006);
});

test('hidden page stops dashboard polling and refreshes on return',()=>{
 const page=pageHarness();const {context,ui}=page;ui.loading=false;
 let requests=0;context.fetch=()=>{requests++;return new Promise(()=>{});};
 context.document.hidden=true;context.window.intervals.at(-1)();assert.equal(requests,0);
 context.document.hidden=false;context.document.handlers.visibilitychange();assert.equal(requests,1);
});

test('an old mode history response cannot release ownership of a newer request',async()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context,element}=page;let oldResolve,newResolve,requests=0;
 context.fetch=path=>{
  if(!path.includes('trade-records'))return new Promise(()=>{});
  requests++;return new Promise(resolve=>{if(requests===1)oldResolve=resolve;else newResolve=resolve;});
 };
 const old=context.loadTradeRecordsForTest();
 element('#mode-toggle').handlers.click();
 const current=context.loadTradeRecordsForTest();
 const reply=(quantity,head)=>({ok:true,headers:{get(){return null;}},async text(){return JSON.stringify({generation:'g',head:`g:${head}`,totalCount:1,nextCursor:'g:1',records:[{id:'row',type:'OPEN',firstSeq:1,quantity}]});}});
 oldResolve(reply('2',2));await old;
 assert.equal(ui.tradeRecordsLoading,true);
 await context.loadTradeRecordsForTest();assert.equal(requests,2);
 newResolve(reply('3',3));await current;
 assert.equal(ui.tradeRecordsLoading,false);assert.equal(ui.tradeRecords[0].quantity,'3');assert.equal(ui.recordHead,'g:3');
});

test('mode switch invalidates category markup cache after clearing nodes',()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context,element}=page;ui.dashboard.marketScan={diagnostics:{availableCategories:[{id:'1',label:'Sports'}]}};
 context.renderCategoriesForTest(ui.preferences);assert.equal(element('#category-options').dataset.markup.includes('data-category-id="1"'),true);
 element('#mode-toggle').handlers.click();assert.equal(element('#category-options').dataset.markup,undefined);
});

test('an external reset clears history immediately and rejects its late response',async()=>{
 const page=pageHarness();seedTestDashboard(page);
 const {ui,context}=page;ui.dashboard.generation='old';ui.recordGeneration='old';ui.recordHead='old:1';ui.recordCursor='old:1';ui.tradeRecords=[{id:'old-fill',type:'OPEN',firstSeq:1}];
 let resolve;context.fetch=path=>path.includes('trade-records')?new Promise(r=>{resolve=r;}):new Promise(()=>{});
 const pending=context.loadTradeRecordsForTest();
 context.applyDashboardForTest({executionMode:'TEST',generation:'new',details:'summary',liveStrategyStatus:'LOCKED',preferences:{...profile},strategy:{status:'PAUSED',initialCapital:'100',availableCash:'100'},positions:[],positionCount:0,positionBooksReady:true,portfolio:{},marketScan:{events:[],diagnostics:{availableCategories:[]}}});
 assert.equal(ui.tradeRecords.length,0);assert.equal(ui.recordHead,null);assert.equal(ui.tradeRecordsLoading,false);
 resolve({ok:true,headers:{get(){return null;}},async text(){return JSON.stringify({generation:'old',head:'old:2',totalCount:1,records:[{id:'old-fill',type:'OPEN',firstSeq:1}]});}});
 await pending;assert.equal(ui.tradeRecords.length,0);assert.equal(ui.recordHead,null);
});

function seedLiveWallet(page,approval={status:'MISSING'}) {
 seedTestDashboard(page);
 const {ui,context}=page;
 ui.displayMode='LIVE';ui.loading=false;
 ui.dashboard={...ui.dashboard,executionMode:'LIVE',details:'summary',generation:'g',liveExecutionEnabled:false,positionCount:0,positions:[],portfolio:{},marketScan:{events:[],diagnostics:{availableCategories:[]}}};
 ui.wallet={configured:true,unlocked:true,enabled:false,status:'BLOCKED',readiness:{ready:false,checks:[]},redemptionApproval:approval};
 ui.walletAt=Date.now();
 context.renderLiveWallet();
}

const walletReply=(body,ok=true)=>({ok,headers:{get(){return null;}},async text(){return JSON.stringify(body);}});

test('wallet action errors survive read refreshes and clear after the next successful action',async()=>{
 const page=pageHarness();seedLiveWallet(page);
 const {ui,context,element}=page;const wallet=ui.wallet;
 context.fetch=async path=>path.endsWith('/approve-redemption')
  ?walletReply({error:'签名钱包 POL 不足，尚未准备授权交易'},false)
  :walletReply(path.startsWith('/api/dashboard')?ui.dashboard:wallet);
 await element('#wallet-approve-redemption').handlers.click();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(ui.walletActionError,'签名钱包 POL 不足，尚未准备授权交易');
 assert.equal(ui.walletError,null);
 assert.match(element('#wallet-message').textContent,/上次操作：签名钱包 POL 不足/);
 // A later successful poll still cannot erase the specific action failure.
 await context.loadLiveWallet();
 assert.match(element('#wallet-message').textContent,/上次操作：签名钱包 POL 不足/);
 context.fetch=async path=>walletReply(path.startsWith('/api/dashboard')?ui.dashboard:wallet);
 await context.postLiveWallet('check',{},element('#wallet-check'),'检查中','账户检查已更新');
 assert.equal(ui.walletActionError,null);
 assert.equal(element('#wallet-message').textContent.includes('POL 不足'),false);
});

test('historical wallet action errors do not block a fresh verified account, while read errors do',async()=>{
 const page=pageHarness();seedLiveWallet(page);
 const {ui,context,element}=page;
 ui.walletActionError='上次操作失败';
 ui.dashboard.liveExecutionEnabled=true;
 const wallet={...ui.wallet,enabled:true,status:'PAUSED',readiness:{ready:true,checks:[]},redemptionApproval:{status:'APPROVED'}};
 context.fetch=async()=>walletReply(wallet);
 await context.loadLiveWallet();
 assert.equal(context.liveAccountVisible(),true);
 assert.equal(context.liveStartAvailable(),true);
 assert.match(element('#wallet-message').textContent,/上次操作失败/);
 context.fetch=async()=>walletReply({error:'controlled read failure'},false);
 await context.loadLiveWallet();
 assert.equal(context.liveAccountVisible(),false);
 assert.equal(context.liveStartAvailable(),false);
 assert.match(element('#wallet-message').textContent,/钱包状态读取失败：controlled read failure/);
 assert.match(element('#wallet-message').textContent,/上次操作失败/);
 element('#mode-toggle').handlers.click();
 assert.equal(ui.walletActionError,null);
});

test('pending authorization displays its safe network result and public transaction identity',async()=>{
 for (const networkError of [false,true]) {
  const page=pageHarness();seedLiveWallet(page);
  const {ui,context,element}=page;ui.walletActionError='previous failure';
  const hash='0x'+'a'.repeat(64);
  const wallet={...ui.wallet,redemptionApproval:{status:'PENDING',networkError,pendingTxHash:hash}};
  context.fetch=async path=>walletReply(path.startsWith('/api/dashboard')?ui.dashboard:wallet);
  await element('#wallet-approve-redemption').handlers.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(ui.walletActionError,null);
  assert.match(element('#wallet-message').textContent,networkError?/网络结果暂未确认/:/尚待链上确认/);
  assert.match(element('#wallet-message').textContent,new RegExp(hash));
  assert.match(element('#message').textContent,networkError?/网络结果暂未确认/:/尚待链上确认/);
  assert.equal(element('#message').className,networkError?'error':'success');
  assert.equal(element('#wallet-approve-redemption').hidden,false);
  assert.equal(element('#wallet-approve-redemption').textContent,'继续核对赎回授权');
  assert.equal(element('#wallet-enable').disabled,true);
  assert.equal(context.liveStartAvailable(),false);
  ui.wallet.redemptionApproval.pendingTxHash='invalid-private-marker';
  context.renderLiveWallet();
  assert.equal(element('#wallet-message').textContent.includes('invalid-private-marker'),false);
 }
});

test('safe authorization preparation failure is displayed even with an HTTP success response',async()=>{
 const page=pageHarness();seedLiveWallet(page);
 const {ui,context,element}=page;
 const message='签名钱包 Polygon POL 不足，无法准备赎回授权交易。';
 const wallet={...ui.wallet,redemptionApproval:{status:'FAILED',networkError:false,pendingTxHash:null,errorCode:'INSUFFICIENT_POL',errorMessage:message,stage:'prepare'}};
 context.fetch=async path=>walletReply(path.startsWith('/api/dashboard')?ui.dashboard:wallet);
 await element('#wallet-approve-redemption').handlers.click();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(element('#wallet-message').textContent,message);
 assert.equal(element('#message').textContent,message);
 assert.equal(element('#message').className,'error');
 assert.equal(element('#wallet-approve-redemption').textContent,'重试赎回授权');
 assert.equal(element('#wallet-enable').disabled,true);
});

test('successful backup restoration clears historical wallet action errors',async()=>{
 const page=pageHarness();seedLiveWallet(page);
 const {ui,context,element}=page;
 context.TextEncoder=TextEncoder;context.Blob=Blob;
 ui.wallet={configured:false,unlocked:false,enabled:false,status:'UNCONFIGURED'};
 ui.walletActionError='previous restoration failure';
 element('#wallet-restore-file').files=[new Blob(['controlled encrypted backup'])];
 element('#wallet-restore-password').value='controlled-backup-password';
 const wallet={configured:true,unlocked:false,enabled:false,status:'LOCKED',redemptionApproval:null};
 context.fetch=async path=>path.endsWith('/restore')
  ?{...walletReply(wallet),async json(){return wallet;}}
  :walletReply(path.startsWith('/api/dashboard')?ui.dashboard:wallet);
 await element('#wallet-restore-form').handlers.submit({preventDefault(){}});
 assert.equal(ui.walletActionError,null);
 assert.equal(ui.walletError,null);
 assert.equal(ui.wallet.status,'LOCKED');
 assert.equal(element('#wallet-message').textContent.includes('previous restoration failure'),false);
});

test('obsolete wallet reads cannot replace the result or error of a newer action',async()=>{
 const page=pageHarness();seedLiveWallet(page);
 const {ui,context}=page;let resolveRead;
 context.fetch=()=>new Promise(resolve=>{resolveRead=resolve;});
 const pending=context.loadLiveWallet();
 const wallet={...ui.wallet,redemptionApproval:{status:'PENDING',pendingTxHash:'0x'+'a'.repeat(64)}};
 ui.walletEpoch++;ui.wallet=wallet;ui.walletActionError='new action failure';
 resolveRead(walletReply({...wallet,status:'READY',readiness:{ready:true}}));
 await pending;
 assert.equal(ui.wallet,wallet);
 assert.equal(ui.walletActionError,'new action failure');
 assert.equal(ui.wallet.readiness.ready,false);
});

test('wallet import submits a blank address for Safe discovery and rejects malformed explicit input',async()=>{
 for (const address of ['', 'bad-address', '0x'+'2'.repeat(40)]) {
  const page=pageHarness();seedLiveWallet(page);
  const {ui,context,element}=page;
  ui.wallet={configured:false,unlocked:false,enabled:false,status:'UNCONFIGURED'};
  element('input[name="wallet-kind"]:checked').value='private_key';
  element('#wallet-private-key').value='0x'+'1'.repeat(64);
  element('#wallet-public-address').value=address;
  element('#wallet-account-index').value='0';
  element('#wallet-vault-password').value=element('#wallet-vault-confirm').value='controlled-test-password';
  const writes=[];
  context.fetch=async(path,options={})=>{
   if(options.method==='POST')writes.push({path,body:JSON.parse(options.body)});
   return walletReply(path.startsWith('/api/dashboard')?ui.dashboard:{configured:true,unlocked:true,enabled:false,status:'BLOCKED',signatureType:2,readiness:{ready:false,checks:[]}});
  };
  element('#wallet-import-form').handlers.submit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,address==='bad-address'?0:1);
  if (writes.length) {
   assert.equal(writes[0].path,'/api/live/wallet/import');
   assert.equal(writes[0].body.publicAddress,address);
   assert.equal(ui.wallet.enabled,false);
   assert.equal(element('#wallet-enable').disabled,true);
  }
 }
});
