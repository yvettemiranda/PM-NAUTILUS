import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness,profile,seedTestDashboard} from './ui-app-harness.mjs';
import {displayStatus,curveSegments,modeSwitchDecision,walletOriginSecure,liveWalletAccountVerified,liveRunSummary,redemptionApprovalAction,panelVisible,setMarkupIfChanged} from '../src/pm_nautilus/web/ui-state.js';
const base=()=>({executionMode:'TEST',strategy:{status:'RUNNING'},positions:[{currentSellPriceStatus:'NO_BID'}],marketScan:{lastScanAt:'2026-09-22',diagnostics:{streams:{groups:[{taskRunning:true,state:'READY',readyBookCount:2,tokenCount:2}]}}}});
const opts={lastSuccess:1000,now:2000};
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
 context.fetch=(path)=>path===url?new Promise(resolve=>{requests++;resolveMutation=resolve;}):new Promise(()=>{});
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
 context.fetch=path=>path==='/api/test/preferences'?new Promise(resolve=>{resolveSort=resolve;}):new Promise(()=>{});
 const pending=element('#sort-toggle').handlers.click();
 ui.displayMode='LIVE';ui.mutationVersion++;ui.preferences=null;ui.dashboard=null;
 const message=element('#message').textContent;
 resolveSort({ok:false,headers:{get(){return null;}},async text(){return JSON.stringify({error:'old TEST failure'});}});
 await pending;
 assert.equal(element('#message').textContent,message);
 assert.equal(ui.preferences,null);
 assert.equal(element('#sort-toggle').disabled,true);
});
