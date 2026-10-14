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


test('summary readiness includes hidden positions',()=>{
 const dashboard=base();dashboard.positions=[];dashboard.positionBooksReady=false;
 assert.equal(displayStatus(dashboard,{lastSuccess:1000,now:2000}).feed,'waiting');
});

test('summary and market retain verified whole-account position counts without stale details',()=>{
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
   ui.activeTab=details==='summary'?'records':'market';
   context.applyDashboardForTest({...dashboard,details,positions:[]});
   assert.equal(element('#tab-position-count').textContent,'86');
   assert.equal(element('#position-count').textContent,'读取中');
   assert.equal(element('#position-list-controls').hidden,true);
   assert.match(element('#positions').innerHTML,/正在读取持仓状态/);
   assert.equal(element('#positions').innerHTML.includes('LOADED_POSITION'),false);
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
