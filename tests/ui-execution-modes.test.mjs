import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness, profile, seedTestDashboard} from './ui-app-harness.mjs';

const reply = body => ({ok:true, headers:{get(){return null;}}, async text(){return JSON.stringify(body);}});
const tick = async () => { for(let i=0;i<15;i++) await Promise.resolve(); };
function snapshot(accountId=1, extra={}) {
  return {executionMode:'TEST', accountId, generation:'g', details:'positions', activeExecutionMode:'LIVE', testExecutionSuspended:true,
    preferences:{...profile}, strategy:{status:'PAUSED',initialCapital:'100',availableCash:'100'}, capitalEditable:true,
    portfolio:{totalFunds:'100',availableCash:'100'}, positions:[], positionCount:0,
    marketScan:{events:[],diagnostics:{availableCategories:[]}}, ...extra};
}
function pageAt(accountId=1) {
  const page=pageHarness({savedAccount:accountId});seedTestDashboard(page);
  page.ui.loading=false;page.ui.performanceAt=Date.now();page.ui.performance={accountId,generation:'g',points:[]};
  return page;
}
const controls = ['run-toggle','reset-test','save-config','sort-toggle'];

test('a suspended TEST dashboard disables all writes while account switching remains available',async()=>{
  const page=pageAt(10);page.context.applyDashboardForTest(snapshot(10));
  for(const id of controls)assert.equal(page.element(`#${id}`).disabled,true,id);
  assert.equal(page.element('#initial-capital').disabled,true);
  assert.equal(page.element('#test-account-select').disabled,false);
  assert.equal(page.element('#config-toggle').disabled,false);
  const writes=[];page.context.fetch=async(path,options)=>{writes.push({path,options});return reply(snapshot(10));};
  for(const id of ['run-toggle','reset-test','sort-toggle'])await page.element(`#${id}`).handlers.click();
  await page.element('#config-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(writes,[]);
  assert.match(page.element('#message').textContent,/TEST后台已停用/);
});

test('intentional TEST shutdown replaces stale feed/recovery and continued-exit explanations',()=>{
  const page=pageAt();
  page.context.applyDashboardForTest(snapshot(1,{
    positions:[{tokenId:'owned',marketQuestion:'KEPT_POSITION',quantity:'1',remainingCost:'1',targetSellPrices:[],stopLossThreshold:null,currentSellPriceStatus:'NOT_READY'}],
    positionCount:1, positionBooksReady:false,
    portfolio:{estimatedTotalFunds:'100',availableCash:'99',unvaluedPositionCount:1,unpricedPositionCost:'1'},
    marketScan:{events:[],scanning:true,serviceError:'ValueError',streamError:'disconnected',diagnostics:{availableCategories:[],streams:{groups:[{state:'STOPPED',taskRunning:false,error:'old disconnect'}]}}},
  }));
  assert.match(page.element('#connection-warning').textContent,/TEST后台已停用/);
  assert.doesNotMatch(page.element('#connection-warning').textContent,/行情正在恢复|自动暂停/);
  assert.match(page.element('#runtime-status').textContent,/不再接收行情.*退出、结算/);
  assert.doesNotMatch(page.element('#runtime-status').textContent,/仍继续退出/);
  assert.equal(page.element('#run-state-label').textContent,'后台已停用');
  assert.equal(page.element('#module-status-label').textContent,'TEST后台已停用');
  assert.equal(page.element('#scan-refresh-state').textContent,'TEST后台已停用');
  assert.equal(page.element('#scan-refresh-state').classList.contains('is-refreshing'),false);
  assert.equal(page.element('#run-dot').dataset.state,'off');
  assert.equal(page.element('#positions-dot').dataset.state,'off');
  assert.equal(page.element('#service-warning').hidden,true);
  assert.match(page.element('#positions').innerHTML,/KEPT_POSITION/);
  assert.match(page.element('#positions').innerHTML,/TEST后台已停用/);
  assert.doesNotMatch(page.element('#positions').innerHTML,/恢复报价后/);
  assert.match(page.element('#valuation-detail').textContent,/TEST后台已停用/);
  assert.doesNotMatch(page.element('#valuation-detail').textContent,/恢复报价后/);
});

test('ordinary PAUSED TEST keeps the existing controls and exit/settlement explanation',()=>{
  const page=pageAt();page.context.applyDashboardForTest(snapshot(1,{testExecutionSuspended:false,activeExecutionMode:'TEST'}));
  for(const id of controls)assert.equal(page.element(`#${id}`).disabled,false,id);
  assert.match(page.element('#runtime-status').textContent,/已有仓位仍继续退出和赎回/);
  assert.equal(page.element('#run-state-label').textContent,'已暂停');
});

test('a successful backend rollback re-enables TEST controls without initiating START',()=>{
  const page=pageAt();page.context.applyDashboardForTest(snapshot());
  const requests=[];page.context.fetch=async(path,options)=>{requests.push({path,options});return reply({});};
  page.context.applyDashboardForTest(snapshot(1,{testExecutionSuspended:false,activeExecutionMode:'TEST'}));
  for(const id of controls)assert.equal(page.element(`#${id}`).disabled,false,id);
  assert.equal(requests.length,0);
});

test('TEST suspension metadata never disables LIVE configuration or changes LIVE status text',()=>{
  const page=pageAt();page.ui.displayMode='LIVE';
  const live={...snapshot(),executionMode:'LIVE',liveExecutionEnabled:false,testExecutionSuspended:false};
  page.context.applyDashboardForTest(live);
  const before=controls.map(id=>page.element(`#${id}`).disabled);
  const text=page.element('#runtime-status').textContent;
  page.context.applyDashboardForTest({...live,testExecutionSuspended:true});
  assert.deepEqual(controls.map(id=>page.element(`#${id}`).disabled),before);
  assert.equal(page.element('#runtime-status').textContent,text);
  assert.doesNotMatch(text,/TEST后台已停用/);
  assert.equal(page.element('#save-config').disabled,false);
});

test('a late suspended reply from another account cannot block the current account',async()=>{
  const page=pageAt();page.context.applyDashboardForTest(snapshot(1,{testExecutionSuspended:false}));
  const requests=[];page.context.fetch=(path,options)=>new Promise(resolve=>requests.push({path,options,resolve}));
  const old=page.context.loadDashboard();
  const selector=page.element('#test-account-select');selector.value='2';selector.handlers.change({target:selector});
  page.ui.performanceAt=Date.now();page.ui.performance={accountId:2,generation:'g',points:[]};
  requests[1].resolve(reply(snapshot(2,{testExecutionSuspended:false})));await tick();
  requests[0].resolve(reply(snapshot(1)));await old;
  assert.equal(page.ui.dashboard.accountId,2);
  for(const id of controls)assert.equal(page.element(`#${id}`).disabled,false,id);
  assert.doesNotMatch(page.element('#runtime-status').textContent,/TEST后台已停用/);
});

test('a stale suspended snapshot still exposes a current data refresh failure',()=>{
  const page=pageAt();page.context.applyDashboardForTest(snapshot());
  page.ui.dashboardError='offline';page.context.renderStatus();
  assert.match(page.element('#connection-warning').textContent,/数据更新中断/);
  assert.equal(page.element('#run-state-label').textContent,'需关注');
  for(const id of controls)assert.equal(page.element(`#${id}`).disabled,true,id);
});

const restorable = (accountId=1) => snapshot(accountId, {
  liveExecutionEnabled:false, activeExecutionMode:'STOPPED', modeSwitchInProgress:false, canRestoreTestBackground:true,
});

test('stopped TEST with no enabled LIVE reuses the run button only for restoring its background',()=>{
  const page=pageAt(10);page.context.applyDashboardForTest(restorable(10));
  const button=page.element('#run-toggle');
  assert.equal(button.disabled,false);assert.equal(button.textContent,'↺');
  assert.equal(button.title,'恢复TEST后台（保持暂停）');
  assert.equal(button.getAttribute('aria-label'),'恢复TEST后台（保持暂停）');
  for(const id of ['reset-test','save-config','sort-toggle'])assert.equal(page.element(`#${id}`).disabled,true,id);
});

test('restoring TEST background posts an explicit account-bound confirmation and never STARTs BUY',async()=>{
  const page=pageAt(10);page.context.applyDashboardForTest(restorable(10));
  const requests=[];
  const restored=snapshot(10,{testExecutionSuspended:false,activeExecutionMode:'TEST',liveExecutionEnabled:false,modeSwitchInProgress:false,canRestoreTestBackground:false});
  page.context.fetch=async(path,options)=>{requests.push({path,options});return reply(restored);};
  await page.element('#run-toggle').handlers.click();
  const writes=requests.filter(request=>request.options.method==='POST');
  assert.equal(writes.length,1);
  const url=new URL(writes[0].path,'https://localhost');
  assert.equal(url.pathname,'/api/test/restore-background');
  assert.equal(url.searchParams.get('account_id'),'10');
  assert.deepEqual(JSON.parse(writes[0].options.body),{confirmation:'RESTORE TEST'});
  assert.equal(requests.some(request=>new URL(request.path,'https://localhost').pathname.endsWith('/start')),false);
  assert.equal(page.ui.strategyStatus,'PAUSED');
  assert.match(page.element('#message').textContent,/全部账户保持暂停，未启动新增买入/);
  assert.equal(page.ui.dashboard.testExecutionSuspended,false);
});

test('LIVE enabled, transitioning or unconfirmed restore permission keep the recovery button disabled',async()=>{
  for(const extra of [
    {liveExecutionEnabled:true}, {modeSwitchInProgress:true}, {modeSwitchInProgress:undefined},
    {canRestoreTestBackground:false}, {canRestoreTestBackground:undefined},
  ]) {
    const page=pageAt();page.context.applyDashboardForTest({...restorable(),...extra});
    assert.equal(page.element('#run-toggle').disabled,true);
    const requests=[];page.context.fetch=async(path)=>{requests.push(path);return reply({});};
    await page.element('#run-toggle').handlers.click();
    assert.deepEqual(requests,[]);
    for(const id of ['reset-test','save-config','sort-toggle'])assert.equal(page.element(`#${id}`).disabled,true,id);
  }
});

test('a refused background restore preserves suspended state and cannot enable save reset or sort',async()=>{
  const page=pageAt();page.context.applyDashboardForTest(restorable());
  page.context.fetch=async()=>({ok:false,headers:{get(){return null;}},async text(){return JSON.stringify({error:'LIVE尚未确认关闭'});}});
  await page.element('#run-toggle').handlers.click();
  assert.equal(page.ui.dashboard.testExecutionSuspended,true);
  assert.match(page.element('#message').textContent,/LIVE尚未确认关闭/);
  assert.equal(page.element('#run-toggle').getAttribute('aria-label'),'恢复TEST后台（保持暂停）');
  assert.equal(page.ui.strategyStatus,'PAUSED');
  for(const id of ['reset-test','save-config','sort-toggle'])assert.equal(page.element(`#${id}`).disabled,true,id);
});
